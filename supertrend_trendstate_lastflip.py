import os
import json
import tempfile
from datetime import datetime
import pytz

import yfinance as yf
import pandas as pd
import pandas_ta as ta
import gspread
from google.oauth2.service_account import Credentials

# ======================================================
# CONFIG
# ======================================================
SPREADSHEET_ID = "1tDRsk2B9udGRhirzwxrryc3HvtHhg6rwN8UTjeJlwAc"
INPUT_SHEET = "StockList"
OUTPUT_SHEET = "Supertrend_Status"

ATR_PERIOD = 10
ATR_MULTIPLIER = 3
START_DATE = "2015-01-01"
RECENT_DAYS = 4

# ======================================================
# SECTOR ENRICHMENT (APPEND-ONLY)
# ======================================================
ETF_SECTOR_MAP = {
    "NIFTYBEES": "Index",
    "BANKBEES": "Banking",
    "ITBEES": "IT",
    "PHARMABEES": "Pharma",
    "FMCGIETF": "FMCG",
    "METALIETF": "Metals",
    "PSUBNKBEES": "PSU Banks",
    "PVTBANIETF": "Private Banks",
    "OILIETF": "Energy",
    "MIDCAPETF": "Midcap",
    "JUNIORBEES": "Midcap",
    "MOM30IETF": "Momentum",
    "LOWVOLIETF": "Low Volatility",
    "NV20IETF": "Value",
    "ALPHA": "Alpha",
    "MODEFENCE": "Defence",
    "MAFANG": "Global Tech",
    "MON100": "Nasdaq 100",
    "GOLDBEES": "Gold",
    "SILVERBEES": "Silver",
    "ICICIB22": "PSU",
    "HDFCSML250": "Small Cap",
    "BFSI": "Financial Services",
    "HEALTHY": "Healthcare",
    "MOM100": "Momentum",
    "MOMENTUM50": "Momentum",
    "MONIFTY500": "Index",
    "MIDSMALL": "Index",
    "MULTICAP": "Index",
    "SMALLCAP": "Index",
    "NIFTYQLITY": "Quality",
    "MOQUALITY": "Quality",
    "MOVALUE": "Value",
    "MOLOWVOL": "Low Volatility",
    "EMULTIMQ": "Multi Factor",
    "MNC": "MNC",
    "EVINDIA": "Electric Vehicles",
    "MAKEINDIA": "Manufacturing",
    "ESG": "ESG",
}

REIT_MAP = {"EMBASSY", "MINDSPACE", "BIRET", "MOREALTY"}
INVIT_MAP = {"INDIGRID", "IRBINVIT", "PGINVIT", "SHREMINVIT"}

# ======================================================
# GOOGLE AUTH
# ======================================================
SERVICE_ACCOUNT_INFO = json.loads(os.environ["GOOGLE_CREDENTIALS"])

tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
with open(tmp.name, "w") as f:
    json.dump(SERVICE_ACCOUNT_INFO, f)

scopes = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_file(tmp.name, scopes=scopes)
client = gspread.authorize(creds)
sheet = client.open_by_key(SPREADSHEET_ID)

# ======================================================
# SCAN METADATA
# ======================================================
ist = pytz.timezone("Asia/Kolkata")
scan_time = datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S")
scan_source = "GitHub Automation" if os.getenv("GITHUB_ACTIONS") == "true" else "Manual Run"

# ======================================================
# READ SYMBOL LIST
# ======================================================
symbols = sheet.worksheet(INPUT_SHEET).col_values(1)[1:]
symbols = [s.strip() for s in symbols if s.strip()]
print(f"Loaded {len(symbols)} symbols")

rows = []

# ======================================================
# FUNDAMENTALS (UNCHANGED)
# ======================================================
def get_fundamentals(yf_symbol):
    try:
        info = yf.Ticker(yf_symbol).info
        mc = info.get("marketCap")

        size = ""
        if mc:
            if mc >= 5e11:
                size = "Large Cap"
            elif mc >= 5e10:
                size = "Mid Cap"
            else:
                size = "Small Cap"

        return {
            "sector": info.get("sector", ""),
            "size": size,
            "mcap": round(mc / 1e7, 2) if mc else "",
            "pe": round(info.get("trailingPE"), 2) if info.get("trailingPE") else "",
            "pb": round(info.get("priceToBook"), 2) if info.get("priceToBook") else "",
            "roe": round(info.get("returnOnEquity") * 100, 2) if info.get("returnOnEquity") else "",
            "de": round(info.get("debtToEquity"), 2) if info.get("debtToEquity") else "",
            "dy": round(info.get("dividendYield") * 100, 2) if info.get("dividendYield") else "",
        }
    except Exception:
        return {k: "" for k in ["sector","size","mcap","pe","pb","roe","de","dy"]}

# ======================================================
# MAIN LOOP
# ======================================================
for symbol in symbols:
    try:
        core = symbol.replace(".NS", "").upper()
        yf_symbol = core + ".NS"
        nse_symbol = f"NSE:{core}"

        df = yf.download(yf_symbol, start=START_DATE, auto_adjust=False, progress=False)
        if df.empty:
            continue

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df.dropna(subset=["High", "Low", "Close"])
        if len(df) < 30:
            continue

        df.ta.supertrend(length=ATR_PERIOD, multiplier=ATR_MULTIPLIER, append=True)
        dcol = [c for c in df.columns if c.startswith("SUPERTd")][0]

        last = df.iloc[-1]

        # =========================
        # TREND STATE
        # =========================
        trend_state = "Green" if int(last[dcol]) == 1 else "Red"

        # =========================
        # FLIP LOGIC
        # =========================
        st_flip_today = False
        last_flip_date = ""
        days_since_flip = ""
        action = ""

        for i in range(len(df) - 1, 0, -1):
            if int(df.iloc[i - 1][dcol]) != int(df.iloc[i][dcol]):
                last_flip_date = df.index[i].date()
                days_since_flip = len(df) - i - 1
                direction = "BUY" if int(df.iloc[i][dcol]) == 1 else "SELL"
                if days_since_flip == 0:
                    st_flip_today = True
                    action = direction
                elif 1 <= days_since_flip <= RECENT_DAYS:
                    action = f"Recent {direction} Signal"
                break

        # =========================
        # SMA LOGIC
        # =========================
        df["SMA20"] = df["Close"].rolling(20).mean()
        df["SMA200"] = df["Close"].rolling(200).mean()

        above_20 = "Yes" if last["Close"] > df["SMA20"].iloc[-1] else "No"
        above_200 = "" if pd.isna(df["SMA200"].iloc[-1]) else ("Yes" if last["Close"] > df["SMA200"].iloc[-1] else "No")

        pyramiding_eligible = "Yes" if trend_state == "Green" and above_20 == "Yes" else "No"

        # =========================
        # VOLUME LOGIC (ORIGINAL)
        # =========================
        volume_spike_today = "NO"
        volume_accumulation = "NO"

        if "Volume" in df.columns and len(df) >= 25:
            vol_avg = df["Volume"].rolling(20).mean()
            if last["Volume"] >= 2 * vol_avg.iloc[-2]:
                volume_spike_today = "YES"

            recent_vol = df["Volume"].iloc[-6:-1]
            recent_avg = vol_avg.iloc[-6:-1]
            if (recent_vol >= 1.5 * recent_avg).sum() >= 3:
                volume_accumulation = "YES"

        fund = get_fundamentals(yf_symbol)

        # =========================
        # SECTOR OVERRIDE (APPEND-ONLY)
        # =========================
        if core in ETF_SECTOR_MAP:
            fund["sector"] = ETF_SECTOR_MAP[core]
            fund["size"] = "ETF"
        elif core in REIT_MAP:
            fund["sector"] = "REIT"
            fund["size"] = "Trust"
        elif core in INVIT_MAP:
            fund["sector"] = "InvIT"
            fund["size"] = "Trust"

        rows.append([
            symbol,
            nse_symbol,
            fund["sector"],
            fund["size"],
            fund["mcap"],
            fund["pe"],
            fund["pb"],
            fund["roe"],
            fund["de"],
            fund["dy"],
            round(last["Close"], 2),
            trend_state,
            st_flip_today,
            above_20,
            above_200,
            pyramiding_eligible,
            volume_spike_today,
            volume_accumulation,
            last_flip_date,
            action,
            days_since_flip
        ])

    except Exception as e:
        print(f"Error on {symbol}: {e}")

# ======================================================
# OUTPUT (ORIGINAL ORDER)
# ======================================================
df_out = pd.DataFrame(rows, columns=[
    "Stock",
    "NSE Symbol",
    "Sector",
    "Size",
    "Market Cap (₹ Cr)",
    "PE (TTM)",
    "PB",
    "ROE (%)",
    "Debt/Equity",
    "Dividend Yield (%)",
    "Close",
    "Trend State",
    "ST Flip Today",
    "Above 20 SMA",
    "Above 200 SMA",
    "Pyramiding Eligible",
    "Volume Spike Today",
    "Volume Accumulation (5D)",
    "Last Flip Date",
    "Action",
    "Days Since Flip"
])

ws = sheet.worksheet(OUTPUT_SHEET)
ws.clear()
ws.update(
    range_name="A1",
    values=[
        ["Last Scan Time (IST)", scan_time],
        ["Scan Source", scan_source],
        [],
        df_out.columns.tolist()
    ] + df_out.astype(str).values.tolist()
)

print("\n=== SCAN COMPLETE ===")
print(f"Rows written: {len(df_out)}")
print(f"Scan Time (IST): {scan_time}")
