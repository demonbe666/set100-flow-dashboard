"""
SET100 Money Flow + Paper Trade Dashboard
==============================
สำหรับวิเคราะห์ flow รายวันของหุ้น SET100 และจำลอง paper trade แบบวาง Bid ที่โซน Avg Low
(High/Low) เฉลี่ยกี่ SD โดยใช้ฐาน SD ที่คำนวณครั้งเดียว (fixed) จาก daily return
ทั้งช่วงเวลาที่เลือก (ไม่ใช่ rolling 90-day แบบ dashboard Money Flow เดิม)

Methodology
-----------
1. ดึงราคาปิดรายวัน (Close) ของแต่ละหุ้นย้อนหลัง LOOKBACK_MONTHS เดือน
2. คำนวณ daily return = Close / PrevClose - 1  สำหรับทุกวันในช่วงนั้น
3. SD = std(daily return) ของทั้งช่วง (ค่าเดียว ไม่ rolling) -> นี่คือ "1 SD" ของหุ้นตัวนั้น
4. สำหรับแต่ละวัน แปลง High และ Low เป็นหน่วย SD เทียบกับ PrevClose:
       High_SD = (High - PrevClose) / (PrevClose * SD)
       Low_SD  = (Low  - PrevClose) / (PrevClose * SD)
   ตัวอย่าง: ถ้า High_SD = +0.55 แปลว่าวันนั้นราคาขึ้นไปแตะ +0.55 เท่าของ 1SD จาก
   ราคาปิดเมื่อวันก่อน
5. เฉลี่ย High_SD และ Low_SD ของทุกวันในช่วง -> ได้ "กรอบเคลื่อนไหวเฉลี่ยรายวัน"
   ในหน่วย SD ของหุ้นตัวนั้น เช่น ABC เคลื่อนเฉลี่ยระหว่าง -0.48SD ถึง +0.52SD
6. Range_SD = High_SD - Low_SD เฉลี่ย = ความกว้างกรอบเฉลี่ยรวม (ยิ่งมากยิ่งมีช่วงให้เทรด
   ระหว่างวันมาก เทียบกับความผันผวนพื้นฐานของหุ้นตัวเอง)

หมายเหตุสำคัญ: รายชื่อ SET100 ในไฟล์ set100_tickers.txt เป็นรายชื่อโดยประมาณ ณ ช่วงเวลาที่เขียนโค้ด
(SET ปรับรายชื่อทุก 6 เดือน - รอบ ม.ค.-มิ.ย. และ ก.ค.-ธ.ค.) กรุณาตรวจสอบ/แก้ไข
รายชื่อให้ตรงกับรายชื่อ SET100 ปัจจุบันจาก
https://www.set.or.th/en/market/information/securities-list/constituents-list-set50-set100
ก่อนใช้งานจริง

วิธีใช้
-------
1. pip install flask yfinance pandas numpy
2. python app.py
3. เปิด http://127.0.0.1:5001
"""

from flask import Flask, render_template_string, request, redirect, url_for, jsonify
import json
import yfinance as yf
import pandas as pd
import numpy as np
import os
import shutil
import threading
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo
import certifi
from paper_store import PaperStateConflict, PaperStateStore

app = Flask(__name__)
paper_state_store = PaperStateStore()

YF_CACHE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "Temp",
    "yfinance-cache",
)
os.makedirs(YF_CACHE_DIR, exist_ok=True)
yf.cache.set_cache_location(YF_CACHE_DIR)

CERT_FILE = os.path.join(YF_CACHE_DIR, "cacert.pem")
if not os.path.exists(CERT_FILE):
    shutil.copyfile(certifi.where(), CERT_FILE)
os.environ["SSL_CERT_FILE"] = CERT_FILE
os.environ["REQUESTS_CA_BUNDLE"] = CERT_FILE
os.environ["CURL_CA_BUNDLE"] = CERT_FILE

# ---------------------------------------------------------------------------
# รายชื่อ SET100
# ---------------------------------------------------------------------------
DEFAULT_SET100_TICKERS = [
    "ADVANC", "AOT", "AWC", "BANPU", "BBL", "BCP", "BDMS", "BEM", "BH", "BJC",
    "BTS", "CBG", "CCET", "COM7", "CPALL", "CPF", "CPN", "CRC", "DELTA",
    "EGCO", "GLOBAL", "GPSC", "GULF", "HMPRO", "IVL", "KBANK", "KCE", "KKP",
    "KTB", "KTC", "LH", "MINT", "MTC", "OR", "OSP", "PTT", "PTTEP", "PTTGC",
    "RATCH", "SAWAD", "SCB", "SCC", "SCGP", "SPALI", "TIDLOR", "TISCO",
    "TLI", "TOP", "TRUE", "TTB", "TU", "WHA", "AAV", "AEONTS", "AMATA",
    "AP", "BA", "BAM", "BCH", "BCPG", "BGRIM", "CHG", "CK", "CKP", "DOHOME",
    "EA", "ERW", "GUNKUL", "HANA", "IRPC", "ITC", "JAS", "JMT", "JMART",
    "MEGA", "PLANB", "PR9", "PTG", "QH", "SAPPE", "SIRI", "SISB", "SJWD",
    "SPRC", "STA", "STGT", "TASCO", "TOA", "TKN", "VGI", "WHAUP", "MASTER",
    "MBK", "PSH", "RBF", "SAV", "SNNP", "THANI", "TIPH", "ORI",
]


def load_ticker_universe():
    ticker_file = os.path.join(os.path.dirname(__file__), "set100_tickers.txt")
    try:
        with open(ticker_file, "r", encoding="utf-8") as fh:
            loaded = [
                line.strip().upper()
                for line in fh
                if line.strip() and not line.lstrip().startswith("#")
            ]
    except OSError:
        loaded = []

    seen = set()
    tickers = []
    for symbol in loaded or DEFAULT_SET100_TICKERS:
        clean = "".join(ch for ch in symbol if ch.isalnum())
        if clean and clean not in seen:
            seen.add(clean)
            tickers.append(clean)
    return tickers


TICKERS = load_ticker_universe()

SECTOR_MAP = {
    "ADVANC": "ICT",
    "AOT": "TRANS",
    "AWC": "PROP",
    "BANPU": "ENERG",
    "BBL": "BANK",
    "BDMS": "HELTH",
    "BEM": "TRANS",
    "BH": "HELTH",
    "BTS": "TRANS",
    "CBG": "FOOD",
    "CCET": "ETRON",
    "COM7": "COMM",
    "CPALL": "COMM",
    "CPF": "FOOD",
    "CPN": "PROP",
    "CRC": "COMM",
    "DELTA": "ETRON",
    "EGCO": "ENERG",
    "GLOBAL": "COMM",
    "GPSC": "ENERG",
    "GULF": "ENERG",
    "HMPRO": "COMM",
    "IVL": "PETRO",
    "KBANK": "BANK",
    "KCE": "ETRON",
    "KTB": "BANK",
    "KTC": "FIN",
    "LH": "PROP",
    "MINT": "TOURISM",
    "MTC": "FIN",
    "OR": "ENERG",
    "OSP": "FOOD",
    "PTT": "ENERG",
    "PTTEP": "ENERG",
    "PTTGC": "PETRO",
    "RATCH": "ENERG",
    "SAWAD": "FIN",
    "SCB": "BANK",
    "SCC": "CONMAT",
    "SCGP": "PKG",
    "SPALI": "PROP",
    "TIDLOR": "FIN",
    "TISCO": "BANK",
    "TLI": "INSUR",
    "TOP": "ENERG",
    "TRUE": "ICT",
    "TTB": "BANK",
    "TU": "FOOD",
    "WHA": "PROP",
}

LOOKBACK_MONTHS = 6  # ระยะเวลาย้อนหลังที่ใช้คำนวณ (ตามที่ระบุ)
LOOKBACK_PERIOD = f"{LOOKBACK_MONTHS}mo"

# cache ผลลัพธ์ในหน่วยความจำ (โหลดทีละ 50 ticker ใช้เวลาหลายสิบวินาที ไม่อยาก
# โหลดใหม่ทุกครั้งที่ refresh หน้าเว็บ)
_cache = {"data": None, "errors": None, "ts": None, "date": None}
_performance_cache = {"data": None, "errors": None, "ts": None}

# cache แยกสำหรับ Money Flow รายวัน (ข้อมูล intraday เปลี่ยนทุกนาที ควร refresh
# บ่อยกว่า cache ของ SD ด้านบน จึงแยก cache คนละตัว)
_flow_cache = {"data": None, "errors": None, "ts": None}
_quote_cache = {}

# Intraday flow changes during the trading day. Keep this cache short so a
# long-lived Render process cannot keep showing the previous trading session.
FLOW_CACHE_MAX_AGE_SECONDS = 60
FLOW_DOWNLOAD_TIMEOUT_SECONDS = 8
FLOW_BATCH_SIZE = int(os.environ.get("FLOW_BATCH_SIZE", "10"))
FLOW_DOWNLOAD_THREADS = os.environ.get("FLOW_DOWNLOAD_THREADS", "0") == "1"
FLOW_PROBE_SYMBOLS = ("PTT", "GULF", "DELTA")
PERFORMANCE_CACHE_MAX_AGE_SECONDS = 15 * 60
_flow_refresh_lock = threading.Lock()


def now_th_str() -> str:
    return datetime.now(ZoneInfo("Asia/Bangkok")).strftime("%Y-%m-%d %H:%M:%S")

# ---------------------------------------------------------------------------
# ตาราง Spread / Tick Size ของ SET (มีผลตั้งแต่ 30 มี.ค. 2552 เป็นต้นไป)
# ใช้แปลงราคา Low/High ที่คำนวณจาก SD ให้เป็นราคาจริงที่ "เทรดได้" ตามช่วงราคา
# ---------------------------------------------------------------------------
TICK_TABLE = [
    (2.0, 0.01),    # ต่ำกว่า 2 บาท
    (5.0, 0.02),    # 2 - <5 บาท
    (10.0, 0.05),   # 5 - <10 บาท
    (25.0, 0.10),   # 10 - <25 บาท
    (100.0, 0.25),  # 25 - <100 บาท
    (200.0, 0.50),  # 100 - <200 บาท
    (400.0, 1.00),  # 200 - <400 บาท
    (float("inf"), 2.00),  # >=400 บาท
]


def get_tick_size(price: float) -> float:
    """คืนค่า tick size (ช่วงราคาขั้นต่ำ) ของ SET ตามระดับราคาหุ้น"""
    for upper, tick in TICK_TABLE:
        if price < upper:
            return tick
    return TICK_TABLE[-1][1]


def round_to_tick(price: float, tick: float) -> float:
    """ปัดราคาให้ตรงกับ tick size จริง (ปัดเข้าใกล้ค่าที่คำนวณได้มากที่สุด)"""
    if tick <= 0:
        return price
    return round(round(price / tick) * tick, 2)


def floor_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    return round(np.floor((price / tick) + 1e-9) * tick, 2)


def count_price_ticks(start_price: float, end_price: float) -> int:
    """Count executable SET price steps from start_price up to end_price."""
    current = round(float(start_price), 2)
    target = round(float(end_price), 2)
    if current <= 0 or target <= current:
        return 0
    ticks = 0
    while current < target - 1e-9 and ticks < 100_000:
        current = round(current + get_tick_size(current), 2)
        ticks += 1
    return ticks


def price_after_ticks(price: float, ticks: int) -> float:
    """Move a SET quote upward by executable price ticks."""
    current = round(float(price), 2)
    for _ in range(max(0, int(ticks))):
        current = round(current + get_tick_size(current), 2)
    return current


def price_before_ticks(price: float, ticks: int) -> float:
    """Move a SET quote downward by executable price ticks."""
    current = round(float(price), 2)
    for _ in range(max(0, int(ticks))):
        tick = get_tick_size(max(current - 1e-9, 0.01))
        current = round(max(0.01, current - tick), 2)
    return current


def compute_stock_sd(symbol: str):
    """ดึงข้อมูลและคำนวณค่า SD-range ของหุ้นตัวเดียว"""
    df = yf.download(
        f"{symbol}.BK",
        period=LOOKBACK_PERIOD,
        interval="1d",
        progress=False,
        auto_adjust=False,
    )
    if df is None or df.empty:
        raise ValueError("no data returned")

    # บางเวอร์ชันของ yfinance คืน MultiIndex columns เมื่อดึงทีละตัวก็มี ป้องกันไว้
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df[["Open", "High", "Low", "Close"]].dropna()
    if len(df) < 20:
        raise ValueError(f"data too short ({len(df)} rows)")

    df["prev_close"] = df["Close"].shift(1)
    df = df.dropna(subset=["prev_close"])

    df["ret"] = df["Close"] / df["prev_close"] - 1.0
    sd = df["ret"].std(ddof=1)  # SD คงที่ตัวเดียวจากข้อมูลทั้งช่วง

    if sd == 0 or np.isnan(sd):
        raise ValueError("SD is zero/NaN (illiquid or bad data)")

    df["high_sd"] = (df["High"] - df["prev_close"]) / (df["prev_close"] * sd)
    df["low_sd"] = (df["Low"] - df["prev_close"]) / (df["prev_close"] * sd)
    df["range_sd"] = df["high_sd"] - df["low_sd"]

    last_close = float(df["Close"].iloc[-1])
    recent_14 = df.tail(min(14, len(df)))
    recent_20 = df.tail(min(20, len(df)))
    recent_30 = df.tail(min(30, len(df)))
    today_th = datetime.now(ZoneInfo("Asia/Bangkok")).date()
    completed = df[pd.Series(df.index.date, index=df.index) < today_th]
    if completed.empty:
        completed = df
    completed_low_move = completed["Low"] / completed["prev_close"] - 1.0
    paper_prev_close = float(completed["Close"].iloc[-1])
    paper_avg_low_move_12 = float(
        completed_low_move.tail(min(12, len(completed_low_move))).mean()
    )
    paper_avg_low_move_30 = float(
        completed_low_move.tail(min(30, len(completed_low_move))).mean()
    )

    return {
        "ticker": symbol,
        "last_close": last_close,
        "paper_prev_close": paper_prev_close,
        "paper_avg_low_move_12": paper_avg_low_move_12,
        "paper_avg_low_move_30": paper_avg_low_move_30,
        "paper_level_date": completed.index[-1].date().isoformat(),
        "sd_pct": float(sd * 100),
        "avg_high_sd": float(df["high_sd"].mean()),
        "avg_low_sd": float(df["low_sd"].mean()),
        "avg_low_sd_30": float(recent_30["low_sd"].mean()),
        "avg_range_sd": float(df["range_sd"].mean()),
        "median_range_sd": float(df["range_sd"].median()),
        "upper_sd_14": float(recent_14["high_sd"].quantile(0.75)),
        "upper_sd_20": float(recent_20["high_sd"].quantile(0.70)),
        "upper_sd_30": float(recent_30["high_sd"].quantile(0.90)),
        "range_sd_14": float(recent_14["range_sd"].quantile(0.80)),
        "range_sd_20": float(recent_20["range_sd"].quantile(0.70)),
        "range_sd_30": float(recent_30["range_sd"].quantile(0.80)),
        "max_range_sd": float(df["range_sd"].max()),
        "n_days": int(len(df)),
    }, df["Close"].copy()


def compute_paper_betas(close_histories):
    """Calculate a 60-day beta from the saved SET100 daily-close histories."""
    if not close_histories:
        return {}
    closes = pd.concat(close_histories, axis=1).sort_index()
    closes.columns = list(close_histories)
    today_th = datetime.now(ZoneInfo("Asia/Bangkok")).date()
    closes = closes[pd.Series(closes.index.date, index=closes.index) < today_th]
    returns = closes.pct_change(fill_method=None)
    market_return = returns.mean(axis=1, skipna=True)
    market_variance = market_return.rolling(60, min_periods=40).var()
    beta_frame = returns.rolling(60, min_periods=40).cov(market_return).div(
        market_variance, axis=0
    )
    betas = {}
    for symbol in beta_frame.columns:
        values = beta_frame[symbol].dropna()
        betas[symbol] = float(values.iloc[-1]) if not values.empty else None
    return betas


def load_all(force=False):
    today_th = datetime.now(ZoneInfo("Asia/Bangkok")).date().isoformat()
    if _cache["data"] is not None and not force and _cache.get("date") == today_th:
        return _cache["data"], _cache["errors"], _cache["ts"]

    results = []
    errors = []
    close_histories = {}
    for sym in TICKERS:
        try:
            result, close_history = compute_stock_sd(sym)
            results.append(result)
            close_histories[sym] = close_history
        except Exception as e:
            errors.append({"ticker": sym, "error": str(e)})

    try:
        beta_by_ticker = compute_paper_betas(close_histories)
    except Exception:
        beta_by_ticker = {}
    for result in results:
        result["paper_beta_60d"] = beta_by_ticker.get(result["ticker"])

    results.sort(key=lambda r: r["avg_range_sd"], reverse=True)
    _cache["data"] = results
    _cache["errors"] = errors
    _cache["ts"] = now_th_str()
    _cache["date"] = today_th
    return results, errors, _cache["ts"]


def pct_return(latest: float, base: float) -> float:
    if not base or np.isnan(base):
        return 0.0
    return (latest / base - 1.0) * 100.0


def return_from_bars(closes: pd.Series, bars_back: int) -> float:
    if closes.empty:
        return 0.0
    latest = float(closes.iloc[-1])
    base_idx = -(bars_back + 1)
    base = float(closes.iloc[base_idx]) if len(closes) > bars_back else float(closes.iloc[0])
    return pct_return(latest, base)


def compute_stock_performance(symbol: str):
    df = yf.download(
        f"{symbol}.BK",
        period="1y",
        interval="1d",
        progress=False,
        auto_adjust=False,
    )
    if df is None or df.empty:
        raise ValueError("no data returned")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    closes = df["Close"].dropna()
    if len(closes) < 2:
        raise ValueError(f"data too short ({len(closes)} rows)")

    latest = float(closes.iloc[-1])
    today_th = datetime.now(ZoneInfo("Asia/Bangkok")).date()
    year_start = datetime(today_th.year, 1, 1).date()
    dates = pd.Series(closes.index.date, index=closes.index)
    before_year = closes[dates < year_start]
    in_year = closes[dates >= year_start]
    if not before_year.empty:
        ytd_base = float(before_year.iloc[-1])
    elif not in_year.empty:
        ytd_base = float(in_year.iloc[0])
    else:
        ytd_base = float(closes.iloc[0])

    return {
        "ticker": symbol,
        "last_close": latest,
        "daily_pct": return_from_bars(closes, 1),
        "weekly_pct": return_from_bars(closes, 5),
        "monthly_pct": return_from_bars(closes, 21),
        "ytd_pct": pct_return(latest, ytd_base),
        "last_date": closes.index[-1].date().isoformat(),
        "n_days": int(len(closes)),
    }


def load_performance(force=False):
    cache_age = time.time() - _performance_cache.get("epoch", 0)
    if _performance_cache["data"] is not None and not force and cache_age <= PERFORMANCE_CACHE_MAX_AGE_SECONDS:
        return _performance_cache["data"], _performance_cache["errors"], _performance_cache["ts"]

    results = []
    errors = []
    for sym in TICKERS:
        try:
            results.append(compute_stock_performance(sym))
        except Exception as e:
            errors.append({"ticker": sym, "error": str(e)})

    results.sort(key=lambda r: r["ytd_pct"], reverse=True)
    _performance_cache["data"] = results
    _performance_cache["errors"] = errors
    _performance_cache["ts"] = now_th_str()
    _performance_cache["epoch"] = time.time()
    return results, errors, _performance_cache["ts"]


def build_sector_performance_rows(performance_rows):
    grouped = {}
    for row in performance_rows:
        sector = SECTOR_MAP.get(row["ticker"], "OTHER")
        grouped.setdefault(sector, []).append(row)

    sector_rows = []
    for sector, members in grouped.items():
        count = len(members)
        sector_rows.append({
            "sector": sector,
            "count": count,
            "daily_pct": float(np.mean([m["daily_pct"] for m in members])),
            "weekly_pct": float(np.mean([m["weekly_pct"] for m in members])),
            "monthly_pct": float(np.mean([m["monthly_pct"] for m in members])),
            "ytd_pct": float(np.mean([m["ytd_pct"] for m in members])),
            "last_date": max(m["last_date"] for m in members),
            "members": ", ".join(sorted(m["ticker"] for m in members)),
        })

    return sorted(sector_rows, key=lambda r: r["ytd_pct"], reverse=True)


# ---------------------------------------------------------------------------
# Money Flow รายวัน (วันปัจจุบัน) — ประมาณค่าเงินเข้า/ออกด้วย Tick Rule บนแถบ
# ราคา 1 นาที (เหมือนหลักการ Tick Rule-based Money In/Out ที่ใช้ใน dashboard
# Money Flow ของ Aspen ที่ทำไว้ก่อนหน้านี้): ถ้าราคาปิดของแถบนี้สูงกว่าแถบก่อนหน้า
# ถือว่าแรงซื้อเข้ามา (Money In) มูลค่า = Close*Volume ของแถบนั้น, ถ้าต่ำกว่าถือว่า
# เป็นแรงขาย (Money Out) ถ้าราคาเท่ากัน (no uptick/downtick) ใช้ทิศทางล่าสุดต่อเนื่อง
# (มาตรฐานของ Tick Rule เวลาราคาไม่เปลี่ยน)
# ---------------------------------------------------------------------------

def _extract_batch_frame(batch, yahoo_symbol):
    if batch is None or batch.empty:
        return pd.DataFrame()
    if not isinstance(batch.columns, pd.MultiIndex):
        return batch.copy()

    first_level = batch.columns.get_level_values(0)
    second_level = batch.columns.get_level_values(1)
    if yahoo_symbol in first_level:
        return batch[yahoo_symbol].copy()
    if yahoo_symbol in second_level:
        return batch.xs(yahoo_symbol, axis=1, level=1).copy()
    return pd.DataFrame()


def download_intraday_batch(symbols, period="1d"):
    frames = {}
    batch_size = max(1, int(FLOW_BATCH_SIZE or 10))
    for start in range(0, len(symbols), batch_size):
        chunk = symbols[start:start + batch_size]
        yahoo_symbols = [f"{symbol}.BK" for symbol in chunk]
        batch = yf.download(
            yahoo_symbols,
            period=period,
            interval="1m",
            group_by="ticker",
            progress=False,
            auto_adjust=False,
            threads=FLOW_DOWNLOAD_THREADS,
            timeout=FLOW_DOWNLOAD_TIMEOUT_SECONDS,
        )
        for symbol, yahoo_symbol in zip(chunk, yahoo_symbols):
            frame = _extract_batch_frame(batch, yahoo_symbol)
            if not frame.empty:
                frames[symbol] = frame
    return frames


def probe_today_session():
    """Detect Yahoo's new SET session using only a few liquid symbols."""
    try:
        frames = download_intraday_batch(FLOW_PROBE_SYMBOLS, period="1d")
    except Exception:
        return False, None

    latest_date = None
    for frame in frames.values():
        if "Close" not in frame or "Volume" not in frame:
            continue
        traded = frame[["Close", "Volume"]].dropna()
        traded = traded[traded["Volume"] > 0]
        if traded.empty:
            continue
        index = traded.index
        if index.tz is None:
            index_th = index.tz_localize("UTC").tz_convert("Asia/Bangkok")
        else:
            index_th = index.tz_convert("Asia/Bangkok")
        symbol_date = index_th.date.max()
        latest_date = max(latest_date, symbol_date) if latest_date else symbol_date

    today_th = datetime.now(ZoneInfo("Asia/Bangkok")).date()
    return latest_date == today_th, latest_date.isoformat() if latest_date else None


def flow_probe_retry_seconds():
    now_th = datetime.now(ZoneInfo("Asia/Bangkok"))
    minute = now_th.hour * 60 + now_th.minute
    if 9 * 60 + 55 <= minute <= 10 * 60 + 35:
        return 10
    if 13 * 60 + 50 <= minute <= 14 * 60 + 20:
        return 10
    if 9 * 60 <= minute <= 16 * 60 + 30:
        return 30
    return 60


def compute_money_flow(symbol: str, prev_close: float = None, intraday_df=None):
    """ดึงแถบราคา 1 นาที และประมาณ Money In/Out ด้วย Tick Rule ของ "เซสชันล่าสุด"

    หมายเหตุ: ใช้เซสชัน (วันเทรด) ล่าสุดที่มีข้อมูลจริง ไม่ใช่บังคับว่าต้องเป็น
    วันที่ปัจจุบันตามปฏิทินเท่านั้น เพราะถ้ารันก่อนตลาดเปิด/หลังตลาดปิดในวันนี้
    เซสชันล่าสุดที่มีข้อมูลจะยังเป็นของเมื่อวาน — ถือว่าถูกต้องแล้ว (เหมือนกับที่
    Aspen หรือแพลตฟอร์มอื่นแสดงข้อมูล "เซสชันล่าสุด" ตอนตลาดปิด) พอตลาดเปิดและมี
    การซื้อขายเกิดขึ้นจริง เซสชันล่าสุดจะขยับมาเป็นวันนี้เองโดยอัตโนมัติ
    """
    df = intraday_df.copy() if intraday_df is not None else yf.download(
        f"{symbol}.BK",
        period="1d",
        interval="1m",
        progress=False,
        auto_adjust=False,
        timeout=FLOW_DOWNLOAD_TIMEOUT_SECONDS,
    )
    if df is None or df.empty:
        raise ValueError("ไม่มีข้อมูล intraday เลย (อาจหยุดเทรด/ข้อมูลไม่พร้อม)")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df = df[df["Volume"] > 0]
    if df.empty:
        raise ValueError("ไม่มีแถบที่มีปริมาณซื้อขาย")

    idx = df.index
    if idx.tz is None:
        idx_th = idx.tz_localize("UTC").tz_convert("Asia/Bangkok")
    else:
        idx_th = idx.tz_convert("Asia/Bangkok")

    # เซสชันล่าสุด = วันที่ (ตามเวลาไทย) ที่ล่าสุดที่มีข้อมูลอยู่จริงในชุดข้อมูลนี้
    session_date = idx_th.date.max()
    mask = idx_th.date == session_date
    session_idx_th = idx_th[mask]
    df = df[mask]

    if len(df) < 2:
        raise ValueError(f"ข้อมูลเซสชันล่าสุดมีน้อยเกินไป ({len(df)} แถบ)")

    today_th = datetime.now(ZoneInfo("Asia/Bangkok")).date()
    is_today = session_date == today_th

    diff = df["Close"].diff()
    raw_sign = np.where(diff > 0, 1, np.where(diff < 0, -1, np.nan))
    sign = pd.Series(raw_sign, index=df.index).ffill().fillna(0)

    turnover = df["Close"] * df["Volume"]
    signed_turnover = turnover * sign
    money_in = float(turnover[sign > 0].sum())
    money_out = float(turnover[sign < 0].sum())
    total_turnover = money_in + money_out
    net_flow = money_in - money_out
    prev_net_flow = float(signed_turnover.iloc[:-1].sum())
    flow_abs = abs(net_flow)
    pct_in = (money_in / total_turnover * 100) if total_turnover > 0 else 0.0
    pct_out = 100.0 - pct_in if total_turnover > 0 else 0.0

    last_price = float(df["Close"].iloc[-1])
    session_high = float(df["High"].max())
    session_low = float(df["Low"].min())
    current_range = max(0.0, session_high - session_low)
    latest_bar = df.iloc[-1]

    def close_at_or_before(cutoff):
        positions = [i for i, ts in enumerate(session_idx_th) if ts.time() <= cutoff]
        return float(df["Close"].iloc[positions[-1]]) if positions else None

    morning_exit_price = close_at_or_before(datetime.strptime("12:25", "%H:%M").time())
    afternoon_exit_price = close_at_or_before(datetime.strptime("16:25", "%H:%M").time())
    # ถ้ามี prev_close จาก dataset SD (ราคาปิดอ้างอิง) ใช้คำนวณ Chg/%Chg แบบ
    # เทียบราคาปิดวันก่อนแบบเดียวกับ Aspen ถ้าไม่มีให้ fallback เป็นเทียบกับราคา
    # เปิดของเซสชันนี้แทน (กันไม่ให้ค่าเป็น None จนตารางเรียงลำดับไม่ได้)
    base_price = prev_close if prev_close else float(df["Close"].iloc[0])
    change = last_price - base_price
    pct_change = (change / base_price * 100) if base_price else 0.0

    return {
        "ticker": symbol,
        "last_price": last_price,
        "change": change,
        "pct_change": pct_change,
        "volume": float(df["Volume"].sum()),
        "money_in": money_in,
        "money_out": money_out,
        "net_flow": net_flow,
        "prev_net_flow": prev_net_flow,
        "flow_abs": flow_abs,
        "total_turnover": total_turnover,
        "pct_in": pct_in,
        "pct_out": pct_out,
        "session_high": session_high,
        "session_low": session_low,
        "latest_bar_open": float(latest_bar.get("Open", latest_bar["Close"])),
        "latest_bar_high": float(latest_bar["High"]),
        "latest_bar_low": float(latest_bar["Low"]),
        "latest_bar_close": float(latest_bar["Close"]),
        "market_ts": session_idx_th[-1].isoformat(),
        "morning_exit_price": morning_exit_price,
        "afternoon_exit_price": afternoon_exit_price,
        "current_range": current_range,
        "n_bars": int(len(df)),
        "session_date": session_date.isoformat(),
        "is_today": is_today,
    }


def get_latest_quote(symbol: str):
    cache_key = symbol.upper()
    now = time.time()
    cached = _quote_cache.get(cache_key)
    if cached and now - cached["time"] < 30:
        return cached["data"]

    df = yf.download(
        f"{cache_key}.BK",
        period="5d",
        interval="1m",
        progress=False,
        auto_adjust=False,
    )
    if df is None or df.empty:
        raise ValueError("no intraday quote returned")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df[["Close", "Volume"]].dropna()
    df = df[df["Volume"] > 0]
    if df.empty:
        raise ValueError("no traded bars found")

    idx = df.index
    if idx.tz is None:
        idx_th = idx.tz_localize("UTC").tz_convert("Asia/Bangkok")
    else:
        idx_th = idx.tz_convert("Asia/Bangkok")

    session_date = idx_th.date.max()
    df = df[idx_th.date == session_date]
    if df.empty:
        raise ValueError("no bars in latest session")

    data = {
        "ticker": cache_key,
        "last_price": float(df["Close"].iloc[-1]),
        "volume": float(df["Volume"].sum()),
        "session_date": session_date.isoformat(),
        "ts": now_th_str(),
    }
    _quote_cache[cache_key] = {"time": now, "data": data}
    return data


def synchronized(lock):
    def decorator(function):
        def wrapped(*args, **kwargs):
            with lock:
                return function(*args, **kwargs)
        return wrapped
    return decorator


@synchronized(_flow_refresh_lock)
def load_all_flow(force=False, sd_lookup=None):
    if _flow_cache["data"] is not None and not force:
        cache_age = time.time() - _flow_cache.get("epoch", 0)
        if cache_age <= FLOW_CACHE_MAX_AGE_SECONDS:
            return (
                _flow_cache["data"], _flow_cache["errors"], _flow_cache["ts"],
                _flow_cache.get("session_label"), _flow_cache.get("session_is_today", True),
            )

    sd_lookup = sd_lookup or {}
    results = []
    errors = []
    started = time.perf_counter()
    try:
        intraday_frames = download_intraday_batch(TICKERS)
    except Exception as e:
        intraday_frames = {}
        errors.append({"ticker": "BATCH", "error": str(e)})

    for sym in TICKERS:
        try:
            frame = intraday_frames.get(sym)
            if frame is None or frame.empty:
                raise ValueError("no intraday data returned in batch")
            results.append(compute_money_flow(
                sym,
                prev_close=sd_lookup.get(sym),
                intraday_df=frame,
            ))
        except Exception as e:
            errors.append({"ticker": sym, "error": str(e)})

    results.sort(key=lambda r: r["flow_abs"], reverse=True)

    # หา session_date ที่พบบ่อยที่สุดในผลลัพธ์ทั้งหมด เพื่อแสดงเป็น badge เดียว
    # บอกผู้ใช้ว่าตารางนี้กำลังแสดงข้อมูลของวันไหน (เผื่อบางตัวข้อมูลหลุดเซสชัน)
    session_label = None
    session_is_today = False
    if results:
        from collections import Counter
        date_counts = Counter(r["session_date"] for r in results)
        session_label = date_counts.most_common(1)[0][0]
        session_is_today = any(
            r["session_date"] == session_label and r["is_today"] for r in results
        )

    _flow_cache["data"] = results
    _flow_cache["errors"] = errors
    _flow_cache["ts"] = now_th_str()
    _flow_cache["epoch"] = time.time()
    _flow_cache["session_label"] = session_label
    _flow_cache["session_is_today"] = session_is_today
    _flow_cache["market_ts"] = max(
        (row.get("market_ts") for row in results if row.get("market_ts")),
        default=None,
    )
    _flow_cache["refresh_seconds"] = round(time.perf_counter() - started, 2)
    return results, errors, _flow_cache["ts"], session_label, session_is_today


PAGE = """
<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<title>SET100 Money Flow TOP10 + Paper Trade</title>
<style>
  :root {
    --bg: #0b0e11;
    --panel: #11151a;
    --border: #232932;
    --text: #d7dde3;
    --muted: #6b7785;
    --accent: #ff9f1c;
    --green: #2ecf85;
    --red: #ef4b5e;
    --mono: 'IBM Plex Mono', 'Consolas', monospace;
  }
  * { box-sizing: border-box; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--mono);
    margin: 0;
    padding: 24px 32px 60px;
    font-size: 13px;
  }
  h1 {
    font-size: 18px;
    letter-spacing: 0.5px;
    margin: 0 0 4px;
    color: #fff;
  }
  .sub {
    color: var(--muted);
    font-size: 12px;
    margin-bottom: 18px;
  }
  .meta {
    display: flex;
    gap: 18px;
    align-items: center;
    margin-bottom: 16px;
    flex-wrap: wrap;
  }
  .badge {
    background: var(--panel);
    border: 1px solid var(--border);
    padding: 5px 10px;
    border-radius: 4px;
    color: var(--muted);
  }
  a.refresh {
    background: var(--accent);
    color: #1a1300;
    padding: 6px 14px;
    border-radius: 4px;
    text-decoration: none;
    font-weight: 600;
    font-size: 12px;
  }
  a.refresh:hover { opacity: 0.85; }
  .nav {
    display: flex;
    gap: 8px;
    margin: 12px 0 16px;
    flex-wrap: wrap;
  }
  .nav a {
    border: 1px solid var(--border);
    background: #161b21;
    color: var(--text);
    padding: 7px 12px;
    border-radius: 4px;
    text-decoration: none;
    font-size: 12px;
    font-weight: 600;
  }
  .nav a.active,
  .nav a:hover {
    border-color: var(--accent);
    color: var(--accent);
  }
  table {
    width: 100%;
    border-collapse: collapse;
    background: var(--panel);
    border: 1px solid var(--border);
  }
  th, td {
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
    text-align: right;
    white-space: nowrap;
  }
  th:first-child, td:first-child { text-align: left; }
  th {
    color: var(--muted);
    font-weight: 500;
    cursor: pointer;
    user-select: none;
    position: sticky;
    top: 0;
    background: #161b21;
  }
  th:hover { color: var(--accent); }
  tr:hover td { background: #161b21; }
  .ticker { color: #fff; font-weight: 600; }
  .band-cell { text-align: left; min-width: 220px; }
  .band-track {
    position: relative;
    height: 14px;
    background: #1a2026;
    border-radius: 3px;
    width: 200px;
  }
  .band-zero {
    position: absolute;
    left: 50%;
    top: -2px;
    bottom: -2px;
    width: 1px;
    background: #3a4350;
  }
  .band-fill {
    position: absolute;
    top: 2px;
    bottom: 2px;
    background: linear-gradient(90deg, var(--red), var(--accent), var(--green));
    border-radius: 2px;
    opacity: 0.85;
  }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .err-box {
    margin-top: 24px;
    padding: 10px 14px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 4px;
    color: var(--muted);
    font-size: 11px;
  }
  .err-box summary { cursor: pointer; color: var(--muted); }
  .scale-label { color: var(--muted); font-size: 10px; display:flex; justify-content: space-between; width:200px; }
  h2 {
    font-size: 15px;
    color: #fff;
    margin: 36px 0 4px;
    letter-spacing: 0.3px;
  }
  .flow-cell { text-align: left; min-width: 200px; }
  .flow-track {
    position: relative;
    height: 14px;
    width: 180px;
    border-radius: 3px;
    overflow: hidden;
    display: flex;
    background: #1a2026;
  }
  .flow-in { background: var(--green); height: 100%; }
  .flow-out { background: var(--red); height: 100%; }
  .flow-pct { font-size: 10px; color: var(--muted); margin-top: 2px; }
  .flow-section {
    margin-bottom: 28px;
  }
  .flow-toolbar {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
    margin: -4px 0 12px;
  }
  .performance-toggle {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
    margin: 0 0 12px;
  }
  .performance-toggle button {
    border: 1px solid var(--border);
    border-radius: 4px;
    background: #151a20;
    color: var(--text);
    cursor: pointer;
    font-family: var(--mono);
    font-size: 12px;
    padding: 7px 12px;
  }
  .performance-toggle button.active,
  .performance-toggle button:hover {
    border-color: var(--accent);
    color: var(--accent);
  }
  .performance-section[data-view="stocks"] .sector-performance-table,
  .performance-section[data-view="sector"] .stock-performance-table {
    display: none;
  }
  .follow-panel,
  .follow-alert-panel {
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
    margin: 0 0 12px;
    color: var(--muted);
    font-size: 12px;
  }
  .follow-panel {
    background: #10151b;
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    padding: 10px 12px;
    border-radius: 4px;
  }
  .follow-panel strong {
    color: #fff;
    margin-right: 4px;
  }
  .warmup-status {
    background: #111820;
    border: 1px solid var(--border);
    border-left: 3px solid var(--accent);
    border-radius: 4px;
    color: var(--muted);
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
    margin: 0 0 12px;
    padding: 9px 12px;
    font-size: 12px;
  }
  .warmup-status.ready {
    border-left-color: var(--pos);
    color: var(--pos);
  }
  .follow-panel label {
    display: inline-flex;
    gap: 6px;
    align-items: center;
  }
  .follow-panel input {
    width: 70px;
    background: #0b0e11;
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 5px 7px;
    font-family: var(--mono);
    font-size: 12px;
  }
  .follow-summary {
    color: var(--accent);
    font-weight: 700;
  }
  .sort-chip {
    border: 1px solid var(--border);
    background: #161b21;
    color: var(--text);
    border-radius: 4px;
    padding: 6px 10px;
    font-family: var(--mono);
    font-size: 11px;
    cursor: pointer;
  }
  .sort-chip:hover,
  .sort-chip.active {
    border-color: var(--accent);
    color: var(--accent);
  }
  .watch-table {
    font-size: 13px;
  }
  .watch-table th {
    border-top: 2px solid var(--accent);
  }
  .watch-table td {
    height: 34px;
  }
  .watch-table .ticker {
    border-left: 4px solid transparent;
  }
  .watch-table tr.flow-positive .ticker {
    border-left-color: var(--green);
    color: var(--green);
  }
  .watch-table tr.flow-negative .ticker {
    border-left-color: var(--red);
    color: var(--red);
  }
  .money-flow {
    font-weight: 700;
    font-size: 14px;
  }
  .flow-rank {
    color: var(--muted);
    text-align: center;
  }
  .strong {
    font-weight: 900;
  }
  .bid-setup-section {
    margin-top: 18px;
  }
  #bidtable .trade-entry-head {
    min-width: 110px;
    background: #0d2a20;
    color: #8fffc8;
    font-weight: 800;
    box-shadow: inset 2px 0 0 var(--green), inset -2px 0 0 var(--green);
  }
  #bidtable .trade-entry-cell {
    background: #0a241a;
    color: #35f2a1;
    font-size: 15px;
    font-weight: 900;
    box-shadow: inset 2px 0 0 rgba(46, 207, 133, 0.72), inset -2px 0 0 rgba(46, 207, 133, 0.72);
  }
  #bidtable tr:hover .trade-entry-cell {
    background: #103629;
  }
  #bidtable tr.closest-bid-row td:not(.trade-entry-cell) {
    background: rgba(255, 176, 0, 0.045);
  }
  #bidtable tr.closest-bid-row td:first-child {
    border-left: 3px solid #ffb000;
  }
  .bid-focus-star {
    display: inline-block;
    margin-right: 7px;
    color: #ffbd2e;
    font-size: 16px;
    line-height: 1;
    vertical-align: -1px;
    filter: drop-shadow(0 0 4px rgba(255, 176, 0, 0.5));
  }
  .signal-badge {
    display: inline-flex;
    justify-content: center;
    min-width: 78px;
    border-radius: 4px;
    padding: 3px 7px;
    border: 1px solid var(--border);
    color: var(--muted);
    background: #151a20;
    font-size: 11px;
    font-weight: 700;
  }
  .signal-follow {
    color: #07100c;
    background: var(--green);
    border-color: var(--green);
  }
  .signal-watch {
    color: #1a1300;
    background: var(--accent);
    border-color: var(--accent);
  }
  .signal-flow {
    color: var(--green);
    border-color: rgba(46, 207, 133, 0.55);
  }
  .signal-none {
    color: var(--muted);
  }
  .follow-breakout.ok {
    color: var(--green);
  }
  .follow-breakout.no {
    color: var(--muted);
  }
  tr.follow-ready td {
    background: rgba(46, 207, 133, 0.04);
  }
  tr.follow-watch td {
    background: rgba(255, 159, 28, 0.035);
  }
  .muted-num {
    color: var(--muted);
  }
  .alarm-btn {
    width: 30px;
    height: 26px;
    border-radius: 4px;
    border: 1px solid var(--border);
    background: #161b21;
    color: var(--muted);
    cursor: pointer;
    font-size: 15px;
    line-height: 1;
  }
  .alarm-btn:hover,
  .alarm-btn.active {
    color: var(--accent);
    border-color: var(--accent);
  }
  .alert-panel {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 0 0 12px;
    color: var(--muted);
    font-size: 12px;
    flex-wrap: wrap;
  }
  .alert-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--green);
    display: inline-block;
  }
  .modal-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.72);
    z-index: 40;
    display: none;
    align-items: center;
    justify-content: center;
    padding: 24px;
  }
  .modal-backdrop.show {
    display: flex;
  }
  .modal {
    width: min(460px, 100%);
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 16px;
    box-shadow: 0 20px 80px rgba(0, 0, 0, 0.45);
  }
  .modal h3 {
    margin: 0 0 12px;
    font-size: 15px;
    color: #fff;
  }
  .modal label {
    display: block;
    margin: 10px 0 4px;
    color: var(--muted);
    font-size: 11px;
  }
  .modal input,
  .modal textarea {
    width: 100%;
    background: #0b0e11;
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 8px;
    font-family: var(--mono);
    font-size: 13px;
  }
  .modal textarea {
    min-height: 68px;
    resize: vertical;
  }
  .expiry-quick-row {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin: 7px 0 2px;
  }
  .expiry-chip {
    border: 1px solid var(--border);
    border-radius: 4px;
    background: #151a20;
    color: var(--muted);
    cursor: pointer;
    font-family: var(--mono);
    font-size: 11px;
    padding: 5px 8px;
  }
  .expiry-chip:hover,
  .expiry-chip.active {
    color: var(--accent);
    border-color: var(--accent);
  }
  .modal-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 14px;
  }
  .modal-actions button {
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 7px 12px;
    cursor: pointer;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--text);
    background: #161b21;
  }
  .modal-actions .primary {
    background: var(--accent);
    color: #1a1300;
    border-color: var(--accent);
    font-weight: 700;
  }
  .modal-actions .danger {
    color: var(--red);
  }
  .alert-toast {
    position: fixed;
    right: 20px;
    bottom: 20px;
    z-index: 50;
    width: min(420px, calc(100vw - 40px));
    background: #151a20;
    border: 1px solid var(--accent);
    border-left: 4px solid var(--accent);
    border-radius: 6px;
    padding: 14px 14px 12px;
    box-shadow: 0 18px 60px rgba(0, 0, 0, 0.5);
    display: none;
  }
  .alert-toast.show {
    display: block;
  }
  .alert-toast strong {
    color: #fff;
    display: block;
    margin-bottom: 4px;
  }
  .alert-toast button {
    margin-top: 10px;
    border: 1px solid var(--border);
    background: #0b0e11;
    color: var(--text);
    border-radius: 4px;
    padding: 5px 9px;
    cursor: pointer;
  }
  #flowtable th:nth-child(7),
  #flowtable td:nth-child(7),
  #flowtable th:nth-child(8),
  #flowtable td:nth-child(8) {
    display: none;
  }
  body.view-flow > .sub:first-of-type,
  body.view-flow > .meta:first-of-type,
  body.view-flow .alert-panel,
  body.view-flow #sdtable,
  body.view-flow #sdtable + .scale-label,
  body.view-flow #sdtable + .scale-label + details.err-box,
  body.view-flow .performance-section {
    display: none;
  }
  body.view-sd h2,
  body.view-sd h2 + .sub,
  body.view-sd h2 + .sub + .meta,
  body.view-sd .follow-panel,
  body.view-sd .follow-alert-panel,
  body.view-sd .bid-setup-section,
  body.view-sd .flow-toolbar,
  body.view-sd #flowtable,
  body.view-sd #flowtable + details.err-box,
  body.view-sd .performance-section {
    display: none;
  }
  body.view-performance > .sub:first-of-type,
  body.view-performance > .meta:first-of-type,
  body.view-performance .alert-panel,
  body.view-performance #sdtable,
  body.view-performance #sdtable + .scale-label,
  body.view-performance #sdtable + .scale-label + details.err-box,
  body.view-performance .flow-heading,
  body.view-performance .flow-heading + .sub,
  body.view-performance .flow-heading + .sub + .meta,
  body.view-performance #flowWarmupStatus,
  body.view-performance .bid-setup-section,
  body.view-performance .flow-toolbar,
  body.view-performance #flowtable,
  body.view-performance #flowtable + details.err-box {
    display: none;
  }
  .paper-section { display: none; }
  body.view-paper .paper-section { display: block; }
  body.view-paper > .sub,
  body.view-paper > .meta,
  body.view-paper > .alert-panel,
  body.view-paper > #sdtable,
  body.view-paper > .scale-label,
  body.view-paper > details.err-box,
  body.view-paper > .performance-section,
  body.view-paper > .flow-heading,
  body.view-paper > #flowWarmupStatus,
  body.view-paper > .bid-setup-section,
  body.view-paper > .flow-toolbar,
  body.view-paper > #flowtable {
    display: none;
  }
  .paper-subnav {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 4px 0 12px;
  }
  .paper-subnav a {
    border: 1px solid var(--border);
    border-radius: 4px;
    color: var(--muted);
    padding: 7px 11px;
    text-decoration: none;
  }
  .paper-subnav a:hover { color: #fff; border-color: #657180; }
  .paper-subnav a.active { color: #fff; border-color: var(--accent); }
  .paper-statusbar {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    border-top: 1px solid var(--accent);
    border-bottom: 1px solid var(--border);
    padding: 10px 0;
    margin-bottom: 14px;
  }
  .paper-status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--muted);
    flex: 0 0 auto;
  }
  .paper-status-dot.active { background: var(--green); }
  .paper-status-dot.error { background: var(--red); }
  .paper-status-text { color: #fff; font-weight: 700; }
  .paper-status-detail { color: var(--muted); }
  .paper-actions { margin-left: auto; display: flex; gap: 8px; }
  .paper-icon-btn {
    width: 32px;
    height: 30px;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--panel);
    color: var(--text);
    cursor: pointer;
    font-size: 15px;
  }
  .paper-icon-btn:hover { border-color: var(--accent); color: #fff; }
  .paper-metrics {
    display: grid;
    grid-template-columns: repeat(10, minmax(105px, 1fr));
    border-top: 1px solid var(--border);
    border-bottom: 1px solid var(--border);
    margin-bottom: 16px;
  }
  .paper-metric { padding: 12px 14px; border-right: 1px solid var(--border); }
  .paper-metric:last-child { border-right: 0; }
  .paper-metric-label { color: var(--muted); font-size: 11px; margin-bottom: 5px; }
  .paper-metric-value { color: #fff; font-size: 17px; font-weight: 700; white-space: nowrap; }
  .paper-grid {
    display: grid;
    grid-template-columns: minmax(0, 1.4fr) minmax(340px, 0.8fr);
    gap: 18px;
    margin-bottom: 18px;
  }
  .paper-chart-wrap, .paper-candidates {
    min-width: 0;
    border-bottom: 1px solid var(--border);
    padding-bottom: 12px;
  }
  .paper-section h2 { font-size: 14px; margin: 16px 0 8px; color: #fff; }
  #paperEquityChart { width: 100%; height: 260px; display: block; background: var(--panel); }
  .paper-table-wrap { overflow-x: auto; margin-bottom: 18px; }
  .paper-section table { min-width: 920px; }
  .paper-candidates table { min-width: 0; width: 100%; table-layout: fixed; }
  .paper-candidates th, .paper-candidates td { padding-left: 8px; padding-right: 8px; }
  .paper-empty td { color: var(--muted); text-align: center; padding: 24px; }
  .paper-side-buy { color: var(--green); font-weight: 700; }
  .paper-side-sell { color: var(--red); font-weight: 700; }
  .paper-engine-note { color: var(--muted); font-size: 11px; margin: -2px 0 10px; }
  @media (max-width: 1000px) {
    .paper-metrics { grid-template-columns: repeat(3, 1fr); }
    .paper-metric:nth-child(3) { border-right: 0; }
    .paper-grid { grid-template-columns: 1fr; }
  }
  @media (max-width: 640px) {
    body { padding: 16px 12px 40px; }
    .paper-metrics { grid-template-columns: repeat(2, 1fr); }
    .paper-metric:nth-child(3) { border-right: 1px solid var(--border); }
    .paper-metric:nth-child(even) { border-right: 0; }
    .paper-metric-value { font-size: 15px; }
    .paper-actions { margin-left: 0; }
  }
</style>
</head>
<body class="view-{{ view }}">
  <h1>{{ page_title }}</h1>
  <div class="nav">
    <a class="{{ 'active' if view == 'flow' else '' }}" href="{{ url_for('flow_page') }}">Money Flow TOP10</a>
    <a class="{{ 'active' if view == 'paper' else '' }}" href="{{ url_for('paper_page') }}">Paper Trade</a>
  </div>
  <section
    class="paper-section"
    data-portfolio-id="{{ paper_portfolio_id }}"
    data-portfolios-json='{{ paper_portfolios_json }}'
    data-start-date="{{ paper_start_date }}"
    data-initial-capital="{{ paper_initial_capital }}"
    data-position-budget="{{ paper_position_budget }}"
    data-max-daily-entries="{{ paper_max_daily_entries }}"
    data-commission-rate="{{ paper_commission_rate }}"
  >
    <div class="paper-subnav" aria-label="Paper trade portfolios">
      {% for portfolio in paper_portfolio_nav %}
      <a class="{{ 'active' if paper_portfolio_id == portfolio.id else '' }}" href="{{ url_for('paper_portfolio_page', portfolio_id=portfolio.id) }}">{{ portfolio.short_label }}</a>
      {% endfor %}
    </div>
    <div class="paper-statusbar">
      <span id="paperStatusDot" class="paper-status-dot"></span>
      <span id="paperStatus" class="paper-status-text">WAITING</span>
      <span id="paperStatusDetail" class="paper-status-detail">Start {{ paper_start_date }}</span>
      <div class="paper-actions">
        <button id="paperRefreshBtn" class="paper-icon-btn" type="button" title="Refresh paper engine">↻</button>
        <button id="paperSyncBtn" class="paper-icon-btn" type="button" title="Sync paper portfolio online">☁</button>
        <button id="paperExportBtn" class="paper-icon-btn" type="button" title="Export order log">⇩</button>
        <button id="paperResetBtn" class="paper-icon-btn" type="button" title="Reset paper portfolio">×</button>
      </div>
    </div>

    <div class="paper-metrics">
      <div class="paper-metric"><div class="paper-metric-label">Equity</div><div id="paperEquity" class="paper-metric-value">5,000,000.00</div></div>
      <div class="paper-metric"><div class="paper-metric-label">Cash</div><div id="paperCash" class="paper-metric-value">5,000,000.00</div></div>
      <div class="paper-metric"><div class="paper-metric-label">Market Value</div><div id="paperMarketValue" class="paper-metric-value">0.00</div></div>
      <div class="paper-metric"><div class="paper-metric-label">Realized P&amp;L</div><div id="paperRealized" class="paper-metric-value">0.00</div></div>
      <div class="paper-metric"><div class="paper-metric-label">Unrealized P&amp;L</div><div id="paperUnrealized" class="paper-metric-value">0.00</div></div>
      <div class="paper-metric"><div class="paper-metric-label">Total Return</div><div id="paperReturn" class="paper-metric-value">0.00%</div></div>
      <div class="paper-metric"><div class="paper-metric-label">Win Rate</div><div id="paperWinRate" class="paper-metric-value">-</div></div>
      <div class="paper-metric"><div class="paper-metric-label">Daily Win Rate</div><div id="paperDailyWinRate" class="paper-metric-value">-</div></div>
      <div class="paper-metric"><div class="paper-metric-label">Risk/Reward</div><div id="paperRiskReward" class="paper-metric-value">-</div></div>
      <div class="paper-metric"><div class="paper-metric-label">Profit Factor</div><div id="paperProfitFactor" class="paper-metric-value">-</div></div>
    </div>

    <div class="paper-grid">
      <div class="paper-chart-wrap">
        <h2>Portfolio Equity</h2>
        <canvas id="paperEquityChart"></canvas>
      </div>
      <div class="paper-candidates">
        <h2>Auto Queue &middot; Top {{ paper_top_n }} Flow &middot; {{ paper_strategy_label }}</h2>
        <div class="paper-table-wrap">
          <table id="paperQueueTable">
            <thead><tr><th>Rank</th><th>Ticker</th>{% if paper_beta_aware %}<th>Beta 60D</th>{% endif %}<th>Last</th><th>Bid</th><th>TP</th><th>SL</th></tr></thead>
            <tbody><tr class="paper-empty"><td colspan="{{ 7 if paper_beta_aware else 6 }}">Waiting for market data</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <h2>Open Positions</h2>
    <div class="paper-table-wrap">
      <table id="paperPositionsTable">
        <thead><tr><th>Ticker</th><th>Qty</th><th>Entry</th><th>Last</th><th>TP</th><th>SL</th><th>Flow Rank</th><th>Market Value</th><th>Unrealized</th><th>Session</th></tr></thead>
        <tbody><tr class="paper-empty"><td colspan="10">No open positions</td></tr></tbody>
      </table>
    </div>

    <h2>Order Log</h2>
    <div class="paper-engine-note">Auto scan every 60 seconds while this page is open · {{ paper_strategy_label }} · exit TP, SL, 12:25, or 16:25</div>
    {% if paper_tick_value %}
    <div class="paper-engine-note">Tick value {{ "{:,.0f}".format(paper_tick_value) }} baht/stock &middot; capital {{ "{:,.0f}".format(paper_initial_capital) }} &middot; maximum {{ paper_max_daily_entries }} entries/day &middot; commission 70 baht/million/side</div>
    {% else %}
    <div class="paper-engine-note">Avg Low {{ paper_avg_low_days }} completed days &middot; position budget {{ "{:,.0f}".format(paper_position_budget) }} &middot; maximum {{ paper_max_daily_entries }} entries/day &middot; commission 70 baht/million/side</div>
    {% endif %}
    <div class="paper-table-wrap">
      <table id="paperOrdersTable">
        <thead><tr><th>Market Time</th><th>Side</th><th>Ticker</th><th>Qty</th><th>Price</th><th>Gross Value</th><th>Commission</th><th>Realized P&amp;L</th><th>Reason</th></tr></thead>
        <tbody><tr class="paper-empty"><td colspan="9">No orders</td></tr></tbody>
      </table>
    </div>
  </section>
  <div class="sub">
    กรอบเคลื่อนไหวรายวัน (High/Low) เฉลี่ย ในหน่วย SD และแปลงกลับเป็นราคาจริง &middot;
    ฐาน SD = std(daily return) คงที่จากข้อมูลย้อนหลัง {{ months }} เดือน &middot;
    คอลัมน์ "ราคา Low/High" คำนวณจาก Last Close &times; (1 + SD เฉลี่ย &times; ขนาด 1SD)
    แล้วปัดให้ตรงกับ Tick Size จริงของ SET &middot;
    คอลัมน์ "กรอบ (ช่อง)" สีแดงหมายถึงกรอบแคบกว่า 2 ช่องราคา (ระวังไม่คุ้มค่าคอมฯ/สลิป)
  </div>
  <div class="meta">
    <span class="badge">อัปเดตล่าสุด: {{ ts }}</span>
    <span class="badge">{{ n_ok }} หุ้น (สำเร็จ) / {{ n_err }} หุ้น (ดึงข้อมูลไม่ได้)</span>
    <a class="refresh" href="{{ url_for('refresh') }}">↻ Refresh ข้อมูล</a>
  </div>

  <div class="alert-panel">
    <span class="alert-dot"></span>
    <span id="alertStatus">Alerts: 0 active</span>
    <span>เช็กราคาล่าสุดทุก 60 วินาทีเมื่อเปิดหน้านี้ไว้</span>
  </div>

  <table id="sdtable">
    <thead>
      <tr>
        <th>Alert</th>
        <th data-key="ticker" data-type="str">Ticker</th>
        <th data-key="last_close" data-type="num">Last Close</th>
        <th data-key="sd_pct" data-type="num">1 SD (%)</th>
        <th data-key="avg_low_sd" data-type="num">Avg Low (SD)</th>
        <th data-key="low_price" data-type="num">ราคา Low (เล็งซื้อ)</th>
        <th data-key="avg_high_sd" data-type="num">Avg High (SD)</th>
        <th data-key="high_price" data-type="num">ราคา High (เล็งขาย)</th>
        <th data-key="tick_size" data-type="num">Tick</th>
        <th data-key="range_baht" data-type="num">กรอบ (บาท)</th>
        <th data-key="range_ticks" data-type="num">กรอบ (ช่อง)</th>
        <th data-key="avg_range_sd" data-type="num">Avg Range (SD)</th>
        <th data-key="median_range_sd" data-type="num">Median Range</th>
        <th data-key="n_days" data-type="num">N Days</th>
        <th>กรอบเฉลี่ย (-2SD ถึง +2SD)</th>
      </tr>
    </thead>
    <tbody>
      {% for r in rows %}
      <tr>
        <td>
          <button
            type="button"
            class="alarm-btn price-alarm-btn"
            title="ตั้ง alert เมื่อราคาลงถึง Zone เล็งซื้อ"
            data-ticker="{{ r.ticker }}"
            data-target="{{ "%.2f"|format(r.low_price) }}"
            data-last="{{ "%.2f"|format(r.last_close) }}"
          >⏰</button>
        </td>
        <td class="ticker">{{ r.ticker }}</td>
        <td>{{ "%.2f"|format(r.last_close) }}</td>
        <td>{{ "%.2f"|format(r.sd_pct) }}</td>
        <td class="{{ 'neg' if r.avg_low_sd < 0 else 'pos' }}">{{ "%.2f"|format(r.avg_low_sd) }}</td>
        <td class="neg">{{ "%.2f"|format(r.low_price) }}</td>
        <td class="{{ 'pos' if r.avg_high_sd > 0 else 'neg' }}">+{{ "%.2f"|format(r.avg_high_sd) }}</td>
        <td class="pos">{{ "%.2f"|format(r.high_price) }}</td>
        <td>{{ "%.2f"|format(r.tick_size) }}</td>
        <td>{{ "%.2f"|format(r.range_baht) }}</td>
        <td class="{{ 'neg' if r.range_ticks < 2 else '' }}">{{ r.range_ticks }}</td>
        <td>{{ "%.2f"|format(r.avg_range_sd) }}</td>
        <td>{{ "%.2f"|format(r.median_range_sd) }}</td>
        <td>{{ r.n_days }}</td>
        <td class="band-cell">
          <div class="band-track">
            <div class="band-zero"></div>
            <div class="band-fill" style="left: {{ r.bar_left }}%; width: {{ r.bar_width }}%;"></div>
          </div>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  <div class="scale-label"><span>-2SD</span><span>0</span><span>+2SD</span></div>

  {% if errors %}
  <details class="err-box">
    <summary>หุ้นที่ดึงข้อมูลไม่ได้ ({{ errors|length }})</summary>
    <ul>
      {% for e in errors %}
      <li>{{ e.ticker }} &mdash; {{ e.error }}</li>
      {% endfor %}
    </ul>
  </details>
  {% endif %}

  <div class="performance-section" data-view="stocks">
    <h2>SET100 Performance</h2>
    <div class="sub">
      ผลตอบแทนจากราคาปิดรายวัน: Daily = เทียบวันก่อนหน้า, Weekly = ประมาณ 5 วันทำการ, Monthly = ประมาณ 21 วันทำการ, YTD = เทียบราคาปิดก่อนต้นปีหรือวันแรกของปีที่มีข้อมูล
    </div>
    <div class="meta">
      <span class="badge">อัปเดตล่าสุด: {{ performance_ts }}</span>
      <span class="badge">{{ performance_n_ok }} หุ้น (สำเร็จ) / {{ performance_n_err }} หุ้น (ดึงข้อมูลไม่ได้)</span>
      <a class="refresh" href="{{ url_for('refresh_performance') }}">↻ Refresh Performance</a>
    </div>
    <div class="performance-toggle">
      <button type="button" class="active" data-performance-view="stocks">Stocks</button>
      <button type="button" data-performance-view="sector">Sector</button>
    </div>
    <table id="performancetable" class="sortable watch-table stock-performance-table">
      <thead>
        <tr>
          <th data-key="ticker" data-type="str">Ticker</th>
          <th data-key="last_close" data-type="num">Last Close</th>
          <th data-key="daily_pct" data-type="num">Daily %</th>
          <th data-key="weekly_pct" data-type="num">Weekly %</th>
          <th data-key="monthly_pct" data-type="num">Monthly %</th>
          <th data-key="ytd_pct" data-type="num">YTD %</th>
          <th data-key="last_date" data-type="str">Last Date</th>
        </tr>
      </thead>
      <tbody>
        {% for r in performance_rows %}
        <tr>
          <td class="ticker">{{ r.ticker }}</td>
          <td data-value="{{ "%.4f"|format(r.last_close) }}">{{ "%.2f"|format(r.last_close) }}</td>
          <td class="{{ 'pos' if r.daily_pct >= 0 else 'neg' }}" data-value="{{ "%.4f"|format(r.daily_pct) }}">{{ "%+.2f"|format(r.daily_pct) }}%</td>
          <td class="{{ 'pos' if r.weekly_pct >= 0 else 'neg' }}" data-value="{{ "%.4f"|format(r.weekly_pct) }}">{{ "%+.2f"|format(r.weekly_pct) }}%</td>
          <td class="{{ 'pos' if r.monthly_pct >= 0 else 'neg' }}" data-value="{{ "%.4f"|format(r.monthly_pct) }}">{{ "%+.2f"|format(r.monthly_pct) }}%</td>
          <td class="{{ 'pos' if r.ytd_pct >= 0 else 'neg' }}" data-value="{{ "%.4f"|format(r.ytd_pct) }}">{{ "%+.2f"|format(r.ytd_pct) }}%</td>
          <td data-value="{{ r.last_date }}">{{ r.last_date }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    <table id="sectortable" class="sortable watch-table sector-performance-table">
      <thead>
        <tr>
          <th data-key="sector" data-type="str">Sector</th>
          <th data-key="count" data-type="num">Stocks</th>
          <th data-key="daily_pct" data-type="num">Daily %</th>
          <th data-key="weekly_pct" data-type="num">Weekly %</th>
          <th data-key="monthly_pct" data-type="num">Monthly %</th>
          <th data-key="ytd_pct" data-type="num">YTD %</th>
          <th data-key="last_date" data-type="str">Last Date</th>
          <th data-key="members" data-type="str">Members</th>
        </tr>
      </thead>
      <tbody>
        {% for r in sector_performance_rows %}
        <tr>
          <td class="ticker">{{ r.sector }}</td>
          <td data-value="{{ r.count }}">{{ r.count }}</td>
          <td class="{{ 'pos' if r.daily_pct >= 0 else 'neg' }}" data-value="{{ "%.4f"|format(r.daily_pct) }}">{{ "%+.2f"|format(r.daily_pct) }}%</td>
          <td class="{{ 'pos' if r.weekly_pct >= 0 else 'neg' }}" data-value="{{ "%.4f"|format(r.weekly_pct) }}">{{ "%+.2f"|format(r.weekly_pct) }}%</td>
          <td class="{{ 'pos' if r.monthly_pct >= 0 else 'neg' }}" data-value="{{ "%.4f"|format(r.monthly_pct) }}">{{ "%+.2f"|format(r.monthly_pct) }}%</td>
          <td class="{{ 'pos' if r.ytd_pct >= 0 else 'neg' }}" data-value="{{ "%.4f"|format(r.ytd_pct) }}">{{ "%+.2f"|format(r.ytd_pct) }}%</td>
          <td data-value="{{ r.last_date }}">{{ r.last_date }}</td>
          <td data-value="{{ r.members }}">{{ r.members }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% if performance_errors %}
    <details class="err-box">
      <summary>หุ้นที่ดึง Performance ไม่ได้ ({{ performance_errors|length }})</summary>
      <ul>
        {% for e in performance_errors %}
        <li>{{ e.ticker }} &mdash; {{ e.error }}</li>
        {% endfor %}
      </ul>
    </details>
    {% endif %}
  </div>

  <h2 class="flow-heading">SET100 Money Flow TOP10</h2>
  <div class="sub">
    ประมาณการเงินเข้า/ออกด้วย Tick Rule บนแถบราคา 1 นาที: แถบที่ Close สูงกว่า
    แถบก่อนหน้า &rarr; นับเป็น Money In (มูลค่า = Close &times; Volume ของแถบนั้น),
    แถบที่ Close ต่ำกว่า &rarr; นับเป็น Money Out &middot; Chg/%Chg เทียบกับราคาปิด
    วันก่อนหน้า (จาก dataset SD ด้านบน) &middot; เป็นค่าประมาณจากแถบราคา ไม่ใช่ tick
    จริงทุก order จึงควรใช้เป็นแนวทางคร่าวๆ ไม่ใช่ตัวเลขทางการ
  </div>
  <div class="meta">
    <span class="badge">อัปเดตล่าสุด: {{ flow_ts }}</span>
    {% if flow_market_ts %}
    <span class="badge">แท่งตลาดล่าสุด: {{ flow_market_ts }}</span>
    {% endif %}
    {% if flow_refresh_seconds %}
    <span class="badge">โหลด Flow: {{ flow_refresh_seconds }} วินาที</span>
    {% endif %}
    <span class="badge">{{ flow_n_ok }} หุ้น (สำเร็จ) / {{ flow_n_err }} หุ้น (ดึงข้อมูลไม่ได้)</span>
    {% if session_label %}
      {% if session_is_today %}
        <span class="badge pos">เซสชัน: {{ session_label }} (วันนี้ กำลังเทรด)</span>
      {% else %}
        <span class="badge neg">เซสชัน: {{ session_label }} (ข้อมูลล่าสุดจาก Yahoo &mdash; ยังไม่พบข้อมูลตลาดวันนี้)</span>
      {% endif %}
    {% endif %}
    <a class="refresh" href="{{ url_for('refresh_flow') }}">↻ Refresh Money Flow</a>
  </div>
  <div
    id="flowWarmupStatus"
    class="warmup-status {{ 'ready' if session_is_today else '' }}"
    data-session-today="{{ '1' if session_is_today else '0' }}"
    data-session-label="{{ session_label or '' }}"
  >
    {% if session_is_today %}
      Flow พร้อมใช้: ข้อมูลเป็น session วันนี้แล้ว
    {% else %}
      กำลังรอข้อมูล Flow วันนี้จาก Yahoo: ระบบจะลองเช็กซ้ำให้อัตโนมัติทุก 30 วินาที
    {% endif %}
  </div>

  <div class="flow-section bid-setup-section">
    <h2>Buy Zone Bid Setup</h2>
    <div class="sub">
      Mean reversion setup: Top10 Flow In เท่านั้น &middot; Bid = Avg Low zone ปัดลงตาม tick &middot;
      TP = Prev Close &middot; ต้องวาง bid ตามราคานี้เท่านั้น ไม่ไล่ offer
    </div>
    <div class="follow-alert-panel">
      <span class="alert-dot"></span>
      <span id="bidAlertStatus">Bid alerts: 0 active</span>
      <span>Alert จะดังเมื่อราคาล่าสุดลงถึงหรือต่ำกว่า Bid</span>
    </div>
    <table id="bidtable" class="sortable watch-table">
      <thead>
        <tr>
          <th data-key="flow_in_rank" data-type="num">Flow Rank</th>
          <th data-key="ticker" data-type="str">Ticker</th>
          <th data-key="last_price" data-type="num">Last</th>
          <th class="trade-entry-head" data-key="bid_price" data-type="num">Bid เท่านั้น</th>
          <th data-key="tp_price" data-type="num">TP / Prev Close</th>
          <th data-key="sl_price" data-type="num">SL โยน Bid</th>
          <th data-key="bid_reward_ticks" data-type="num">Reward (ช่อง)</th>
          <th data-key="net_flow" data-type="num">Net Flow (ลบ.)</th>
          <th>Alert</th>
        </tr>
      </thead>
      <tbody>
        {% for r in flow_rows %}
        {% if r.bid_setup_ok %}
        <tr class="{{ 'closest-bid-row' if r.bid_focus_rank else '' }}" data-ticker="{{ r.ticker }}">
          <td data-value="{{ r.flow_in_rank }}">{{ r.flow_in_rank }}</td>
          <td class="ticker">
            {% if r.bid_focus_rank %}
            <span
              class="bid-focus-star"
              title="Closest to Bid #{{ r.bid_focus_rank }} ({{ '%.2f'|format(r.bid_distance_pct) }}%)"
              aria-label="Closest to Bid rank {{ r.bid_focus_rank }}"
            >&#9733;</span>
            {% endif %}
            {{ r.ticker }}
          </td>
          <td data-value="{{ "%.4f"|format(r.last_price) }}">{{ "%.2f"|format(r.last_price) }}</td>
          <td class="trade-entry-cell" data-value="{{ "%.4f"|format(r.bid_price) }}">{{ "%.2f"|format(r.bid_price) }}</td>
          <td class="pos" data-value="{{ "%.4f"|format(r.tp_price) }}">{{ "%.2f"|format(r.tp_price) }}</td>
          <td class="neg strong" data-value="{{ "%.4f"|format(r.sl_price) if r.sl_price is not none else 0 }}">{{ "%.2f"|format(r.sl_price) if r.sl_price is not none else "-" }}</td>
          <td class="pos strong" data-value="{{ r.bid_reward_ticks }}">{{ r.bid_reward_ticks }}</td>
          <td class="{{ 'pos' if r.net_flow >= 0 else 'neg' }}" data-value="{{ "%.4f"|format(r.net_flow / 1e6) }}">{{ "%+.2f"|format(r.net_flow / 1e6) }}</td>
          <td>
            <button
              type="button"
              class="alarm-btn price-alarm-btn bid-alert-btn"
              title="ตั้ง alert เมื่อราคาลงถึง Bid"
              data-ticker="{{ r.ticker }}"
              data-target="{{ "%.2f"|format(r.bid_price) }}"
              data-note="Bid only {{ r.ticker }} @ {{ "%.2f"|format(r.bid_price) }} | TP {{ "%.2f"|format(r.tp_price) }} | SL sell Bid {{ "%.2f"|format(r.sl_price) if r.sl_price is not none else "-" }}"
            >⏰</button>
          </td>
        </tr>
        {% endif %}
        {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="flow-toolbar" data-table="flowtable">
    <button type="button" class="sort-chip" data-sort-key="flow_in_rank" data-sort-dir="asc">Top Flow In</button>
    <button type="button" class="sort-chip active" data-sort-key="flow_abs" data-sort-dir="desc">Flow ใหญ่สุด</button>
    <button type="button" class="sort-chip" data-sort-key="net_flow" data-sort-dir="desc">Money In มากสุด</button>
    <button type="button" class="sort-chip" data-sort-key="net_flow" data-sort-dir="asc">Money Out มากสุด</button>
    <button type="button" class="sort-chip" data-sort-key="total_turnover" data-sort-dir="desc">Value มากสุด</button>
  </div>

  <table id="flowtable" class="sortable watch-table">
    <thead>
      <tr>
        <th data-key="ticker" data-type="str">Ticker</th>
        <th data-key="last_price" data-type="num">Last</th>
        <th data-key="change" data-type="num">Chg</th>
        <th data-key="pct_change" data-type="num">%Chg</th>
        <th data-key="volume_k" data-type="num">Volume (พันหุ้น)</th>
        <th data-key="total_turnover" data-type="num">Turnover (ล.บาท)</th>
        <th data-key="money_in" data-type="num">Money In (ล.บาท)</th>
        <th data-key="money_out" data-type="num">Money Out (ล.บาท)</th>
        <th data-key="net_flow" data-type="num">Net Flow (ล.บาท)</th>
        <th>สัดส่วน In / Out</th>
        <th data-key="flow_in_rank" data-type="num">Flow Rank</th>
        <th data-key="vol_x" data-type="num">Rng20x</th>
        <th data-key="high_zone" data-type="num">20D p70</th>
        <th data-key="upper_30_price" data-type="num">30D p90</th>
        <th data-key="today_high_sd" data-type="num">High SD</th>
        <th data-key="breakout" data-type="num">Breakout</th>
      </tr>
    </thead>
    <tbody>
      {% for r in flow_rows %}
      <tr
        class="{{ 'flow-positive' if r.net_flow >= 0 else 'flow-negative' }}"
        data-ticker="{{ r.ticker }}"
        data-flow-rank="{{ r.flow_in_rank if r.flow_in_rank else 9999 }}"
        data-vol-x="{{ "%.4f"|format(r.vol_x) }}"
        data-last="{{ "%.4f"|format(r.last_price) }}"
        data-high-zone="{{ "%.4f"|format(r.high_zone) if r.high_zone is not none else "" }}"
        data-upper-30="{{ "%.4f"|format(r.upper_30_price) if r.upper_30_price is not none else "" }}"
        data-range-x-30="{{ "%.4f"|format(r.range_x_30) }}"
        data-tick="{{ "%.4f"|format(r.tick_size) }}"
      >
        <td class="ticker">{{ r.ticker }}</td>
        <td>{{ "%.2f"|format(r.last_price) }}</td>
        <td class="{{ 'pos' if r.change >= 0 else 'neg' }}">{{ "%+.2f"|format(r.change) }}</td>
        <td class="{{ 'pos' if r.pct_change >= 0 else 'neg' }}">{{ "%+.2f"|format(r.pct_change) }}%</td>
        <td>{{ r.volume_k_str }}</td>
        <td>{{ "%.2f"|format(r.total_turnover / 1e6) }}</td>
        <td class="pos">{{ "%.2f"|format(r.money_in / 1e6) }}</td>
        <td class="neg">{{ "%.2f"|format(r.money_out / 1e6) }}</td>
        <td class="{{ 'pos' if r.net_flow >= 0 else 'neg' }}">{{ "%+.2f"|format(r.net_flow / 1e6) }}</td>
        <td class="flow-cell">
          <div class="flow-track">
            <div class="flow-in" style="width: {{ r.pct_in_round }}%;"></div>
            <div class="flow-out" style="width: {{ r.pct_out_round }}%;"></div>
          </div>
          <div class="flow-pct">In {{ r.pct_in_round }}% / Out {{ r.pct_out_round }}%</div>
        </td>
        <td class="flow-rank" data-value="{{ r.flow_in_rank if r.flow_in_rank else 9999 }}">{{ r.flow_in_rank if r.flow_in_rank else "-" }}</td>
        <td data-value="{{ "%.4f"|format(r.vol_x) }}">{{ "%.2f"|format(r.vol_x) }}</td>
        <td data-value="{{ "%.4f"|format(r.high_zone) if r.high_zone is not none else 0 }}">{{ "%.2f"|format(r.high_zone) if r.high_zone is not none else "-" }}</td>
        <td data-value="{{ "%.4f"|format(r.upper_30_price) if r.upper_30_price is not none else 0 }}">{{ "%.2f"|format(r.upper_30_price) if r.upper_30_price is not none else "-" }}</td>
        <td data-value="{{ "%.4f"|format(r.today_high_sd) }}">{{ "%.2f"|format(r.today_high_sd) }}</td>
        <td class="follow-breakout {{ 'ok' if r.breakout else 'no' }}" data-value="{{ 1 if r.breakout else 0 }}">{{ "YES" if r.breakout else "NO" }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  {% if flow_errors %}
  <details class="err-box">
    <summary>หุ้นที่ดึง Money Flow ไม่ได้ ({{ flow_errors|length }})</summary>
    <ul>
      {% for e in flow_errors %}
      <li>{{ e.ticker }} &mdash; {{ e.error }}</li>
      {% endfor %}
    </ul>
  </details>
  {% endif %}

  <div id="alertModalBackdrop" class="modal-backdrop">
    <div class="modal">
      <h3 id="alertModalTitle">ตั้ง Price Alert</h3>
      <input type="hidden" id="alertTicker">
      <label for="alertTarget">ราคาแจ้งเตือน (ลงถึงหรือต่ำกว่า)</label>
      <input id="alertTarget" type="number" step="0.01">
      <label for="alertExpiry">วันหมดอายุ</label>
      <input id="alertExpiry" type="datetime-local">
      <div class="expiry-quick-row" data-expiry-input="alertExpiry">
        <button type="button" class="expiry-chip active" data-expiry-mode="session">จบ session</button>
        <button type="button" class="expiry-chip" data-expiry-mode="morning">12:25</button>
        <button type="button" class="expiry-chip" data-expiry-mode="afternoon">16:25</button>
        <button type="button" class="expiry-chip" data-expiry-mode="next">วันถัดไป</button>
      </div>
      <label for="alertNote">โน้ต</label>
      <textarea id="alertNote" placeholder="เช่น รอดู volume / รอ reversal candle"></textarea>
      <div class="modal-actions">
        <button type="button" class="danger" id="deleteAlertBtn">ลบ Alert</button>
        <button type="button" id="cancelAlertBtn">ยกเลิก</button>
        <button type="button" class="primary" id="saveAlertBtn">บันทึก Alert</button>
      </div>
    </div>
  </div>

  <div id="alertToast" class="alert-toast">
    <strong id="alertToastTitle">Alert</strong>
    <div id="alertToastBody"></div>
    <button type="button" id="alertToastClose">ปิด</button>
  </div>

<script>
  const alertStoreKey = 'set100-zone-alerts-v1';
  const followSettingsKey = 'set100-follow-settings-v1';
  const followAlertStoreKey = 'set100-follow-alerts-v1';
  const defaultDocumentTitle = document.title;
  let alertAudioContext = null;

  function loadAlerts() {
    try {
      return JSON.parse(localStorage.getItem(alertStoreKey) || '{}');
    } catch (err) {
      return {};
    }
  }

  function saveAlerts(alerts) {
    localStorage.setItem(alertStoreKey, JSON.stringify(alerts));
  }

  function formatDatetimeLocal(d) {
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }

  function nextWeekdayDate(fromDate = new Date()) {
    const d = new Date(fromDate);
    d.setDate(d.getDate() + 1);
    while (d.getDay() === 0 || d.getDay() === 6) {
      d.setDate(d.getDate() + 1);
    }
    return d;
  }

  function isWeekend(d) {
    return d.getDay() === 0 || d.getDay() === 6;
  }

  function dateAt(baseDate, hour, minute) {
    const d = new Date(baseDate);
    d.setHours(hour, minute, 0, 0);
    return d;
  }

  function expiryDateForMode(mode) {
    const now = new Date();
    if (isWeekend(now)) {
      const nextDay = nextWeekdayDate(now);
      return dateAt(nextDay, mode === 'afternoon' ? 16 : 12, 25);
    }
    if (mode === 'morning') {
      const morning = dateAt(now, 12, 25);
      return now < morning ? morning : dateAt(nextWeekdayDate(now), 12, 25);
    }
    if (mode === 'afternoon') {
      const afternoon = dateAt(now, 16, 25);
      return now < afternoon ? afternoon : dateAt(nextWeekdayDate(now), 16, 25);
    }
    if (mode === 'next') return dateAt(nextWeekdayDate(now), 12, 25);

    const morningEnd = dateAt(now, 12, 25);
    const afternoonEnd = dateAt(now, 16, 25);
    if (now < morningEnd) return morningEnd;
    if (now < afternoonEnd) return afternoonEnd;
    return dateAt(nextWeekdayDate(now), 12, 25);
  }

  function defaultExpiryValue() {
    return formatDatetimeLocal(expiryDateForMode('session'));
  }

  function setExpiryMode(inputId, mode) {
    const input = document.getElementById(inputId);
    if (!input) return;
    const row = document.querySelector(`.expiry-quick-row[data-expiry-input="${inputId}"]`);
    if (!mode) {
      row?.querySelectorAll('.expiry-chip').forEach((chip) => chip.classList.remove('active'));
      return;
    }
    input.value = formatDatetimeLocal(expiryDateForMode(mode));
    row?.querySelectorAll('.expiry-chip').forEach((chip) => {
      chip.classList.toggle('active', chip.dataset.expiryMode === mode);
    });
  }

  document.querySelectorAll('.expiry-chip').forEach((button) => {
    button.addEventListener('click', () => {
      const row = button.closest('.expiry-quick-row');
      setExpiryMode(row?.dataset.expiryInput, button.dataset.expiryMode);
    });
  });

  document.querySelectorAll('.expiry-quick-row').forEach((row) => {
    const input = document.getElementById(row.dataset.expiryInput);
    input?.addEventListener('input', () => setExpiryMode(row.dataset.expiryInput, ''));
  });

  function ensureAudioContext() {
    try {
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      if (!AudioContextClass) return null;
      if (!alertAudioContext) alertAudioContext = new AudioContextClass();
      if (alertAudioContext.state === 'suspended') alertAudioContext.resume();
      return alertAudioContext;
    } catch (err) {
      return null;
    }
  }

  function playAlertSound() {
    const ctx = ensureAudioContext();
    if (!ctx) return;
    [0, 0.22, 0.44].forEach((offset) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.frequency.value = 880;
      gain.gain.setValueAtTime(0.001, ctx.currentTime + offset);
      gain.gain.exponentialRampToValueAtTime(0.18, ctx.currentTime + offset + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + offset + 0.16);
      osc.connect(gain).connect(ctx.destination);
      osc.start(ctx.currentTime + offset);
      osc.stop(ctx.currentTime + offset + 0.18);
    });
  }

  function updateAlertButtons() {
    const alerts = loadAlerts();
    let activeCount = 0;
    document.querySelectorAll('.price-alarm-btn').forEach((button) => {
      const alert = alerts[button.dataset.ticker];
      const isActive = alert && !alert.triggered && new Date(alert.expiry) > new Date();
      button.classList.toggle('active', Boolean(isActive));
      if (isActive) activeCount += 1;
    });
    const status = document.getElementById('alertStatus');
    if (status) status.textContent = `Alerts: ${activeCount} active`;
    const bidStatus = document.getElementById('bidAlertStatus');
    if (bidStatus) bidStatus.textContent = `Bid alerts: ${activeCount} active`;
  }

  function openAlertModal(button) {
    ensureAudioContext();
    const ticker = button.dataset.ticker;
    const alerts = loadAlerts();
    const existing = alerts[ticker] || {};
    const keepExpiry = existing.expiry && !existing.triggered && new Date(existing.expiry) > new Date();
    document.getElementById('alertTicker').value = ticker;
    document.getElementById('alertTarget').value = existing.target ?? button.dataset.target;
    document.getElementById('alertExpiry').value = keepExpiry ? existing.expiry : defaultExpiryValue();
    setExpiryMode('alertExpiry', keepExpiry ? '' : 'session');
    document.getElementById('alertNote').value = existing.note ?? (button.dataset.note || `Zone เล็งซื้อ ${button.dataset.target}`);
    document.getElementById('alertModalTitle').textContent = `${ticker} Price Alert`;
    document.getElementById('deleteAlertBtn').style.display = existing.target ? 'inline-block' : 'none';
    document.getElementById('alertModalBackdrop').classList.add('show');
  }

  function closeAlertModal() {
    document.getElementById('alertModalBackdrop').classList.remove('show');
  }

  function showAlertToast(alert, latestPrice) {
    document.getElementById('alertToastTitle').textContent = `${alert.ticker} ถึง Zone เล็งซื้อ`;
    document.getElementById('alertToastBody').innerHTML =
      `ราคาล่าสุด ${latestPrice.toFixed(2)} ลงถึงเป้า ${Number(alert.target).toFixed(2)}<br>${alert.note || ''}`;
    document.getElementById('alertToast').classList.add('show');
    document.title = `ALERT ${alert.ticker} - ${latestPrice.toFixed(2)}`;
    playAlertSound();
  }

  async function checkOneAlert(alert) {
    if (!alert || alert.triggered) return;
    if (new Date(alert.expiry) <= new Date()) return;
    const response = await fetch(`/api/quote/${encodeURIComponent(alert.ticker)}`, { cache: 'no-store' });
    if (!response.ok) return;
    const quote = await response.json();
    const latestPrice = Number(quote.last_price);
    if (Number.isFinite(latestPrice) && latestPrice <= Number(alert.target)) {
      const alerts = loadAlerts();
      if (alerts[alert.ticker]) {
        alerts[alert.ticker].triggered = true;
        alerts[alert.ticker].triggered_at = new Date().toISOString();
        saveAlerts(alerts);
      }
      showAlertToast(alert, latestPrice);
      updateAlertButtons();
    }
  }

  async function checkAlerts() {
    if (!document.querySelector('.price-alarm-btn')) return;
    const alerts = Object.values(loadAlerts()).filter((alert) =>
      alert && !alert.triggered && new Date(alert.expiry) > new Date()
    );
    for (const alert of alerts) {
      try {
        await checkOneAlert(alert);
      } catch (err) {
        console.warn('alert check failed', alert.ticker, err);
      }
    }
  }

  document.querySelectorAll('.price-alarm-btn').forEach((button) => {
    button.addEventListener('click', () => openAlertModal(button));
  });

  document.getElementById('saveAlertBtn')?.addEventListener('click', () => {
    const ticker = document.getElementById('alertTicker').value;
    const target = Number(document.getElementById('alertTarget').value);
    const expiry = document.getElementById('alertExpiry').value;
    if (!ticker || !Number.isFinite(target) || !expiry) return;
    const alerts = loadAlerts();
    alerts[ticker] = {
      ticker,
      target,
      expiry,
      note: document.getElementById('alertNote').value.trim(),
      created_at: new Date().toISOString(),
      triggered: false,
    };
    saveAlerts(alerts);
    closeAlertModal();
    updateAlertButtons();
    checkAlerts();
  });

  document.getElementById('deleteAlertBtn')?.addEventListener('click', () => {
    const ticker = document.getElementById('alertTicker').value;
    const alerts = loadAlerts();
    delete alerts[ticker];
    saveAlerts(alerts);
    closeAlertModal();
    updateAlertButtons();
  });

  document.getElementById('cancelAlertBtn')?.addEventListener('click', closeAlertModal);
  document.getElementById('alertModalBackdrop')?.addEventListener('click', (event) => {
    if (event.target.id === 'alertModalBackdrop') closeAlertModal();
  });
  document.getElementById('alertToastClose')?.addEventListener('click', () => {
    document.getElementById('alertToast').classList.remove('show');
    document.title = defaultDocumentTitle;
  });

  updateAlertButtons();
  if (document.querySelector('.price-alarm-btn')) {
    checkAlerts();
    setInterval(checkAlerts, 60000);
  }

  let followCheckInFlight = false;

  function loadFollowSettings() {
    const defaults = { topN: 5, volThreshold: 1.0, bufferTicks: 0 };
    try {
      const saved = JSON.parse(localStorage.getItem(followSettingsKey) || '{}');
      return { ...defaults, ...saved };
    } catch (err) {
      return defaults;
    }
  }

  function currentFollowSettings() {
    const saved = loadFollowSettings();
    const topN = Number(document.getElementById('followTopN')?.value ?? saved.topN);
    const volThreshold = Number(document.getElementById('followVolThreshold')?.value ?? saved.volThreshold);
    const bufferTicks = Number(document.getElementById('followBufferTicks')?.value ?? saved.bufferTicks);
    return {
      topN: Number.isFinite(topN) ? Math.max(1, Math.min(20, Math.round(topN))) : saved.topN,
      volThreshold: Number.isFinite(volThreshold) ? Math.max(0.1, volThreshold) : saved.volThreshold,
      bufferTicks: Number.isFinite(bufferTicks) ? Math.round(bufferTicks) : saved.bufferTicks,
    };
  }

  function saveFollowSettings(settings) {
    localStorage.setItem(followSettingsKey, JSON.stringify(settings));
  }

  function loadFollowAlerts() {
    try {
      return JSON.parse(localStorage.getItem(followAlertStoreKey) || '{}');
    } catch (err) {
      return {};
    }
  }

  function saveFollowAlerts(alerts) {
    localStorage.setItem(followAlertStoreKey, JSON.stringify(alerts));
  }

  function resolveFollowState(flowRank, volX, lastPrice, highZone, upper30, rangeX30, tickSize, settings) {
    const rank = Number(flowRank);
    const vol = Number(volX);
    const last = Number(lastPrice);
    const high = Number(highZone);
    const confirmHigh = Number(upper30);
    const range30 = Number(rangeX30);
    const tick = Number(tickSize) || 0;
    const triggerPrice = high + (settings.bufferTicks * tick);
    const topOk = Number.isFinite(rank) && rank > 0 && rank <= settings.topN;
    const volOk = Number.isFinite(vol) && vol >= settings.volThreshold;
    const breakoutOk = Number.isFinite(last) && Number.isFinite(triggerPrice) && last >= triggerPrice;
    const nearHigh = Number.isFinite(last) && Number.isFinite(high) && tick > 0 && last >= high - tick;
    const confirmOk =
      (Number.isFinite(last) && Number.isFinite(confirmHigh) && last >= confirmHigh) ||
      (Number.isFinite(range30) && range30 >= 1.10);

    if (topOk && volOk && breakoutOk) {
      if (confirmOk) {
        return { signal: 'FOLLOW+', className: 'follow', score: 4, breakoutOk, triggerPrice };
      }
      return { signal: 'FOLLOW', className: 'follow', score: 3, breakoutOk, triggerPrice };
    }
    if (topOk && (volOk || nearHigh)) {
      return { signal: 'WATCH', className: 'watch', score: 2, breakoutOk, triggerPrice };
    }
    if (topOk) {
      return { signal: 'TOP FLOW', className: 'flow', score: 1, breakoutOk, triggerPrice };
    }
    return { signal: '-', className: 'none', score: 0, breakoutOk, triggerPrice };
  }

  function stateFromFlowRow(row, settings) {
    const highRaw = row.dataset.highZone;
    const upper30Raw = row.dataset.upper30;
    return resolveFollowState(
      Number(row.dataset.flowRank),
      Number(row.dataset.volX),
      Number(row.dataset.last),
      highRaw === '' ? NaN : Number(highRaw),
      upper30Raw === '' ? NaN : Number(upper30Raw),
      Number(row.dataset.rangeX30),
      Number(row.dataset.tick),
      settings
    );
  }

  function stateFromScanRow(row, settings) {
    return resolveFollowState(
      row.flow_in_rank,
      row.vol_x,
      row.last_price,
      row.high_zone == null ? NaN : row.high_zone,
      row.upper_30_price == null ? NaN : row.upper_30_price,
      row.range_x_30,
      row.tick_size,
      settings
    );
  }

  function applyFollowSettings() {
    if (!document.body.classList.contains('view-flow')) return;
    const settings = currentFollowSettings();
    saveFollowSettings(settings);
    let followCount = 0;
    let watchCount = 0;
    document.querySelectorAll('#flowtable tbody tr').forEach((row) => {
      const state = stateFromFlowRow(row, settings);
      const signalCell = row.querySelector('.follow-score-cell');
      const breakoutCell = row.querySelector('.follow-breakout');

      row.classList.toggle('follow-ready', state.signal === 'FOLLOW');
      row.classList.toggle('follow-watch', state.signal === 'WATCH');
      if (state.signal === 'FOLLOW') followCount += 1;
      if (state.signal === 'WATCH') watchCount += 1;

      if (signalCell) {
        signalCell.dataset.value = state.score;
        signalCell.innerHTML = `<span class="signal-badge signal-${state.className}">${state.signal}</span>`;
      }
      if (breakoutCell) {
        breakoutCell.dataset.value = state.breakoutOk ? 1 : 0;
        breakoutCell.textContent = state.breakoutOk ? 'YES' : 'NO';
        breakoutCell.classList.toggle('ok', state.breakoutOk);
        breakoutCell.classList.toggle('no', !state.breakoutOk);
      }
    });
    const summary = document.getElementById('followSummary');
    if (summary) summary.textContent = `Follow: ${followCount} | Watch: ${watchCount}`;
  }

  function updateFollowAlertButtons() {
    const alerts = loadFollowAlerts();
    let activeCount = 0;
    document.querySelectorAll('.follow-alert-btn').forEach((button) => {
      const alert = alerts[button.dataset.ticker];
      const isActive = alert && !alert.triggered && new Date(alert.expiry) > new Date();
      button.classList.toggle('active', Boolean(isActive));
      if (isActive) activeCount += 1;
    });
    const status = document.getElementById('followAlertStatus');
    if (status) status.textContent = `Follow alerts: ${activeCount} active`;
  }

  function openFollowAlertModal(button) {
    ensureAudioContext();
    const ticker = button.dataset.ticker;
    const alerts = loadFollowAlerts();
    const existing = alerts[ticker] || {};
    const keepExpiry = existing.expiry && !existing.triggered && new Date(existing.expiry) > new Date();
    const settings = existing.settings || currentFollowSettings();
    document.getElementById('followAlertTicker').value = ticker;
    document.getElementById('followAlertExpiry').value = keepExpiry ? existing.expiry : defaultExpiryValue();
    setExpiryMode('followAlertExpiry', keepExpiry ? '' : 'session');
    document.getElementById('followAlertNote').value = existing.note ?? '';
    document.getElementById('followModalTitle').textContent = `${ticker} Follow Buy Alert`;
    document.getElementById('followAlertCondition').textContent =
      `Top ${settings.topN} Flow In + Rng20x >= ${settings.volThreshold} + breakout buffer ${settings.bufferTicks} tick`;
    document.getElementById('deleteFollowAlertBtn').style.display = existing.expiry ? 'inline-block' : 'none';
    document.getElementById('followModalBackdrop').classList.add('show');
  }

  function closeFollowAlertModal() {
    document.getElementById('followModalBackdrop').classList.remove('show');
  }

  function showFollowAlertToast(alert, scanRow, state) {
    const last = Number(scanRow.last_price);
    const vol = Number(scanRow.vol_x);
    document.getElementById('alertToastTitle').textContent = `${alert.ticker} FOLLOW BUY`;
    document.getElementById('alertToastBody').innerHTML =
      `Flow Rank #${scanRow.flow_in_rank || '-'} | Rng20x ${Number.isFinite(vol) ? vol.toFixed(2) : '-'} | Last ${Number.isFinite(last) ? last.toFixed(2) : '-'} >= Trigger ${Number.isFinite(state.triggerPrice) ? state.triggerPrice.toFixed(2) : '-'}<br>${alert.note || ''}`;
    document.getElementById('alertToast').classList.add('show');
    document.title = `FOLLOW ${alert.ticker} - ${Number.isFinite(last) ? last.toFixed(2) : ''}`;
    playAlertSound();
  }

  async function checkFollowAlerts() {
    if (!document.body.classList.contains('view-flow') || followCheckInFlight) return;
    const alerts = Object.values(loadFollowAlerts()).filter((alert) =>
      alert && !alert.triggered && new Date(alert.expiry) > new Date()
    );
    if (!alerts.length) return;
    followCheckInFlight = true;
    try {
      const response = await fetch('/api/follow_scan', { cache: 'no-store' });
      if (!response.ok) return;
      const scan = await response.json();
      const byTicker = new Map((scan.rows || []).map((row) => [row.ticker, row]));
      const stored = loadFollowAlerts();
      alerts.forEach((alert) => {
        const scanRow = byTicker.get(alert.ticker);
        if (!scanRow || stored[alert.ticker]?.triggered) return;
        const state = stateFromScanRow(scanRow, alert.settings || currentFollowSettings());
        if (state.signal === 'FOLLOW') {
          stored[alert.ticker].triggered = true;
          stored[alert.ticker].triggered_at = new Date().toISOString();
          showFollowAlertToast(alert, scanRow, state);
        }
      });
      saveFollowAlerts(stored);
      updateFollowAlertButtons();
    } catch (err) {
      console.warn('follow alert check failed', err);
    } finally {
      followCheckInFlight = false;
    }
  }

  function initFollowBuy() {
    if (!document.body.classList.contains('view-flow')) return;
    const settings = loadFollowSettings();
    const topInput = document.getElementById('followTopN');
    const volInput = document.getElementById('followVolThreshold');
    const bufferInput = document.getElementById('followBufferTicks');
    if (topInput) topInput.value = settings.topN;
    if (volInput) volInput.value = settings.volThreshold;
    if (bufferInput) bufferInput.value = settings.bufferTicks;

    [topInput, volInput, bufferInput].forEach((input) => {
      input?.addEventListener('input', applyFollowSettings);
      input?.addEventListener('change', applyFollowSettings);
    });
    document.querySelectorAll('.follow-alert-btn').forEach((button) => {
      button.addEventListener('click', () => openFollowAlertModal(button));
    });
    document.getElementById('saveFollowAlertBtn')?.addEventListener('click', () => {
      const ticker = document.getElementById('followAlertTicker').value;
      const expiry = document.getElementById('followAlertExpiry').value;
      if (!ticker || !expiry) return;
      const alerts = loadFollowAlerts();
      alerts[ticker] = {
        ticker,
        expiry,
        note: document.getElementById('followAlertNote').value.trim(),
        settings: currentFollowSettings(),
        created_at: new Date().toISOString(),
        triggered: false,
      };
      saveFollowAlerts(alerts);
      closeFollowAlertModal();
      updateFollowAlertButtons();
      checkFollowAlerts();
    });
    document.getElementById('deleteFollowAlertBtn')?.addEventListener('click', () => {
      const ticker = document.getElementById('followAlertTicker').value;
      const alerts = loadFollowAlerts();
      delete alerts[ticker];
      saveFollowAlerts(alerts);
      closeFollowAlertModal();
      updateFollowAlertButtons();
    });
    document.getElementById('cancelFollowAlertBtn')?.addEventListener('click', closeFollowAlertModal);
    document.getElementById('followModalBackdrop')?.addEventListener('click', (event) => {
      if (event.target.id === 'followModalBackdrop') closeFollowAlertModal();
    });
    applyFollowSettings();
    updateFollowAlertButtons();
    checkFollowAlerts();
    setInterval(checkFollowAlerts, 120000);
  }

  const flowColumnMap = {
    ticker: 0,
    last_price: 1,
    change: 2,
    pct_change: 3,
    volume: 4,
    volume_k: 4,
    total_turnover: 5,
    money_in: 6,
    money_out: 7,
    net_flow: 8,
    flow_abs: 8,
    flow_in_rank: 10,
    vol_x: 11,
    high_zone: 12,
    upper_30_price: 13,
    today_high_sd: 14,
    breakout: 15,
    follow_score: 16
  };

  const flowTitle = Array.from(document.querySelectorAll('h2'))
    .find((el) => el.textContent.includes('Money Flow'));
  if (document.body.classList.contains('view-flow') && flowTitle) {
    const firstMeta = document.querySelector('.meta');
    if (firstMeta) {
      const insertBeforeNode = firstMeta.nextSibling;
      const flowNodes = [];
      let node = flowTitle;
      while (node && !(node.nodeType === 1 && node.tagName === 'SCRIPT')) {
        const next = node.nextSibling;
        flowNodes.push(node);
        node = next;
      }
      flowNodes.forEach((flowNode) => {
        firstMeta.parentNode.insertBefore(flowNode, insertBeforeNode);
      });
    }
  }

  function parseCellValue(row, columnIndex, type, key) {
    const cell = row.children[columnIndex];
    if (!cell) return type === 'num' ? 0 : '';
    if (cell.dataset.value !== undefined) {
      return type === 'num' ? Number(cell.dataset.value) : cell.dataset.value;
    }
    const raw = cell.innerText.replace(/[,+%]/g, '').trim();
    const value = type === 'num' ? parseFloat(raw) : raw;
    if (key === 'flow_abs') return Math.abs(Number.isFinite(value) ? value : 0);
    return Number.isFinite(value) || type !== 'num' ? value : 0;
  }

  function sortTable(table, key, dir = 'desc') {
    const headers = Array.from(table.querySelectorAll('th'));
    let idx = headers.findIndex((th) => th.dataset.key === key);
    if (idx < 0 && table.id === 'flowtable') idx = flowColumnMap[key] ?? -1;
    if (idx < 0) return;
    const type = headers[idx]?.dataset.type || (key === 'ticker' ? 'str' : 'num');
    const tbody = table.querySelector('tbody');
    const rows = Array.from(tbody.querySelectorAll('tr'));
    const asc = dir === 'asc';
    rows.sort((a, b) => {
      let av = parseCellValue(a, idx, type, key);
      let bv = parseCellValue(b, idx, type, key);
      if (av < bv) return asc ? -1 : 1;
      if (av > bv) return asc ? 1 : -1;
      return 0;
    });
    rows.forEach((row) => tbody.appendChild(row));
  }

  document.querySelectorAll('#flowtable th').forEach((th) => {
    const key = th.dataset.key;
    if (key === 'total_turnover') th.textContent = 'Value (ลบ.)';
    if (key === 'net_flow') th.textContent = 'Money In/Out (ลบ.)';
  });

  document.querySelectorAll('.flow-toolbar .sort-chip').forEach((button) => {
    button.addEventListener('click', () => {
      const table = document.getElementById(button.closest('.flow-toolbar').dataset.table);
      sortTable(table, button.dataset.sortKey, button.dataset.sortDir);
      button.parentElement.querySelectorAll('.sort-chip').forEach((b) => b.classList.remove('active'));
      button.classList.add('active');
    });
  });

  function initFlowWarmupWatcher() {
    const status = document.getElementById('flowWarmupStatus');
    if (!status || status.dataset.sessionToday === '1') return;
    let attempts = 0;
    const maxAttempts = 360;
    const tick = async () => {
      attempts += 1;
      let retrySeconds = 10;
      try {
        const res = await fetch(`/api/follow_scan?warmup=1&t=${Date.now()}`, { cache: 'no-store' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (data.session_is_today) {
          status.classList.add('ready');
          status.textContent = `Flow พร้อมใช้: พบ session วันนี้แล้ว (${data.session_label}) กำลังรีโหลดหน้า...`;
          setTimeout(() => window.location.reload(), 900);
          return;
        }
        retrySeconds = Number(data.retry_after_seconds || 10);
        const latest = data.session_label || status.dataset.sessionLabel || '-';
        status.textContent = `รอข้อมูล Flow วันนี้จาก Yahoo... ล่าสุดยังเป็น ${latest} · เช็กแล้ว ${attempts}/${maxAttempts}`;
      } catch (err) {
        status.textContent = `รอข้อมูล Flow วันนี้จาก Yahoo... เช็กไม่สำเร็จ (${err.message}) · จะลองใหม่`;
      }
      if (attempts < maxAttempts) {
        setTimeout(tick, Math.max(5, retrySeconds) * 1000);
      } else {
        status.textContent = 'ยังไม่พบข้อมูล Flow วันนี้จาก Yahoo กด Refresh Money Flow เพื่อลองใหม่ได้';
      }
    };
    setTimeout(tick, 3000);
  }

  function initPerformanceToggle() {
    const section = document.querySelector('.performance-section');
    if (!section) return;
    const buttons = Array.from(section.querySelectorAll('[data-performance-view]'));
    const setView = (view) => {
      const nextView = view === 'sector' ? 'sector' : 'stocks';
      section.dataset.view = nextView;
      buttons.forEach((button) => {
        button.classList.toggle('active', button.dataset.performanceView === nextView);
      });
      localStorage.setItem('set100-performance-view-v1', nextView);
    };
    setView(localStorage.getItem('set100-performance-view-v1') || section.dataset.view || 'stocks');
    buttons.forEach((button) => {
      button.addEventListener('click', () => setView(button.dataset.performanceView));
    });
  }

  sortTable(document.getElementById('flowtable'), 'flow_abs', 'desc');
  initFlowWarmupWatcher();
  initPerformanceToggle();
  // sortable table headers (ใช้ได้กับหลายตารางที่มี class="sortable" หรือ id="sdtable")
  document.querySelectorAll('table#sdtable, table.sortable').forEach((table) => {
    const headers = table.querySelectorAll('th');
    let sortState = {};
    headers.forEach((th, idx) => {
      th.addEventListener('click', () => {
        const key = th.dataset.key;
        if (!key) return;
        const type = th.dataset.type;
        const tbody = table.querySelector('tbody');
        const rows = Array.from(tbody.querySelectorAll('tr'));
        const asc = !sortState[key];
        sortState = { [key]: asc };
        rows.sort((a, b) => {
          let av = a.children[idx].innerText.replace('+','').trim();
          let bv = b.children[idx].innerText.replace('+','').trim();
          if (type === 'num') { av = parseFloat(av); bv = parseFloat(bv); }
          if (av < bv) return asc ? -1 : 1;
          if (av > bv) return asc ? 1 : -1;
          return 0;
        });
        rows.forEach(r => tbody.appendChild(r));
      });
    });
  });
</script>
<script src="{{ url_for('static', filename='paper_trade.js', v='20260921-1') }}"></script>
</body>
</html>
"""


def build_rows(results):
    rows = []
    SCALE_MIN, SCALE_MAX = -2.0, 2.0  # ขอบสเกลของแถบแสดงผล
    span = SCALE_MAX - SCALE_MIN
    for r in results:
        lo = max(SCALE_MIN, min(SCALE_MAX, r["avg_low_sd"]))
        hi = max(SCALE_MIN, min(SCALE_MAX, r["avg_high_sd"]))
        if hi < lo:
            hi = lo
        bar_left = (lo - SCALE_MIN) / span * 100
        bar_width = max(1.5, (hi - lo) / span * 100)

        # แปลง SD กลับเป็นราคาจริง โดยอ้างอิงจาก Last Close (ใช้เป็นกรอบคาดการณ์
        # สำหรับวันถัดไป: ราคาเป้าหมายฝั่ง Low = จุดที่เล็งซื้อ, ฝั่ง High = จุดที่
        # เล็งขายตอนเด้งกลับ) แล้วปัดให้ตรงกับ tick size จริงของ SET เพื่อให้เป็น
        # ราคาที่เทรดได้จริง (ไม่ใช่แค่เลขทศนิยมจาก SD ดิบๆ)
        sd_frac = r["sd_pct"] / 100.0
        low_price_raw = r["last_close"] * (1 + r["avg_low_sd"] * sd_frac)
        high_price_raw = r["last_close"] * (1 + r["avg_high_sd"] * sd_frac)
        paper_prev_close = float(r.get("paper_prev_close") or r["last_close"])
        paper_avg_low_move_12 = float(
            r.get("paper_avg_low_move_12") or (r["avg_low_sd_30"] * sd_frac)
        )
        paper_avg_low_move_30 = float(
            r.get("paper_avg_low_move_30") or (r["avg_low_sd_30"] * sd_frac)
        )
        bid_12_avg_raw = paper_prev_close * (1 + paper_avg_low_move_12)
        bid_30_avg_raw = paper_prev_close * (1 + paper_avg_low_move_30)
        upper_14_raw = r["last_close"] * (1 + r["upper_sd_14"] * sd_frac)
        upper_20_raw = r["last_close"] * (1 + r["upper_sd_20"] * sd_frac)
        upper_30_raw = r["last_close"] * (1 + r["upper_sd_30"] * sd_frac)

        tick = get_tick_size(r["last_close"])
        bid_tick_12 = get_tick_size(max(bid_12_avg_raw, 0.01))
        bid_tick = get_tick_size(max(bid_30_avg_raw, 0.01))
        low_price = round_to_tick(low_price_raw, tick)
        high_price = round_to_tick(high_price_raw, tick)
        bid_12_avg = floor_to_tick(bid_12_avg_raw, bid_tick_12)
        bid_30_avg = floor_to_tick(bid_30_avg_raw, bid_tick)
        tp_prev_close = round_to_tick(paper_prev_close, get_tick_size(paper_prev_close))
        upper_14_price = round_to_tick(upper_14_raw, tick)
        upper_20_price = round_to_tick(upper_20_raw, tick)
        upper_30_price = round_to_tick(upper_30_raw, tick)
        if high_price < low_price:
            high_price = low_price
        range_baht = round(high_price - low_price, 2)
        range_ticks = int(round(range_baht / tick)) if tick > 0 else 0
        range_14_baht = round(max(0.0, r["last_close"] * r["range_sd_14"] * sd_frac), 2)
        range_20_baht = round(max(0.0, r["last_close"] * r["range_sd_20"] * sd_frac), 2)
        range_30_baht = round(max(0.0, r["last_close"] * r["range_sd_30"] * sd_frac), 2)

        rows.append({
            **r,
            "bar_left": round(bar_left, 2),
            "bar_width": round(bar_width, 2),
            "low_price": low_price,
            "high_price": high_price,
            "bid_12_avg": bid_12_avg,
            "bid_tick_size_12": bid_tick_12,
            "bid_30_avg": bid_30_avg,
            "bid_tick_size": bid_tick,
            "tp_prev_close": tp_prev_close,
            "upper_14_price": upper_14_price,
            "upper_20_price": upper_20_price,
            "upper_30_price": upper_30_price,
            "tick_size": tick,
            "range_baht": range_baht,
            "range_14_baht": range_14_baht,
            "range_20_baht": range_20_baht,
            "range_30_baht": range_30_baht,
            "range_ticks": range_ticks,
        })
    return rows


FOLLOW_DEFAULT_TOP_N = 5
BUY_ZONE_TOP_N = 10
FOLLOW_DEFAULT_VOL_X = 1.00
PAPER_START_DATE = "2026-07-20"
AVG12_PAPER_START_DATE = "2026-08-03"
FLOW1045_BETA_PAPER_START_DATE = "2026-09-22"
PAPER_INITIAL_CAPITAL = 5_000_000.0
PAPER_POSITION_BUDGET = 1_000_000.0
PAPER_MAX_DAILY_ENTRIES = 5
PAPER_COMMISSION_RATE = 70.0 / 1_000_000.0
PAPER_PORTFOLIOS = {
    "avg30-top5flow": {
        "id": "avg30-top5flow",
        "label": "30avg-top5flow",
        "short_label": "30avg-top5flow",
        "top_n": 5,
        "avg_low_days": 30,
        "start_date": PAPER_START_DATE,
        "bid_field": "bid_price_30",
        "sl_field": "sl_price_30",
        "position_budget": 1_000_000.0,
        "max_daily_entries": 5,
        "storage_key": f"set100-paper-trade-v1-avg30-top5-{PAPER_START_DATE}",
    },
    "avg30-top10flow": {
        "id": "avg30-top10flow",
        "label": "30avg-top10flow",
        "short_label": "30avg-top10flow",
        "top_n": 10,
        "avg_low_days": 30,
        "start_date": PAPER_START_DATE,
        "bid_field": "bid_price_30",
        "sl_field": "sl_price_30",
        "position_budget": 500_000.0,
        "max_daily_entries": 10,
        "storage_key": f"set100-paper-trade-v1-avg30-top10-{PAPER_START_DATE}",
    },
    "avg12-top5flow": {
        "id": "avg12-top5flow",
        "label": "avg12-top5flow",
        "short_label": "avg12-top5flow",
        "top_n": 5,
        "avg_low_days": 12,
        "start_date": AVG12_PAPER_START_DATE,
        "bid_field": "bid_price_12",
        "sl_field": "sl_price_12",
        "position_budget": 1_000_000.0,
        "max_daily_entries": 5,
        "storage_key": f"set100-paper-trade-v1-avg12-top5-{AVG12_PAPER_START_DATE}",
    },
    "avg12-top10flow": {
        "id": "avg12-top10flow",
        "label": "avg12-top10flow",
        "short_label": "avg12-top10flow",
        "top_n": 10,
        "avg_low_days": 12,
        "start_date": AVG12_PAPER_START_DATE,
        "bid_field": "bid_price_12",
        "sl_field": "sl_price_12",
        "position_budget": 500_000.0,
        "max_daily_entries": 10,
        "storage_key": f"set100-paper-trade-v1-avg12-top10-{AVG12_PAPER_START_DATE}",
    },
    "flow1045-beta-top5": {
        "id": "flow1045-beta-top5",
        "label": "flow10:45-beta-top5",
        "short_label": "flow10:45 beta",
        "top_n": 5,
        "avg_low_days": 0,
        "strategy": "fixed_time_bid_entry",
        "strategy_label": "10:45 Beta TP1/TP2 · SL2",
        "entry_minute": 10 * 60 + 45,
        "entry_label": "10:45",
        "start_date": FLOW1045_BETA_PAPER_START_DATE,
        "bid_field": "instant_bid_price",
        "sl_field": "instant_sl_price",
        "position_budget": PAPER_INITIAL_CAPITAL,
        "max_daily_entries": 5,
        "position_sizing": "tick_value",
        "tick_value": 2_000.0,
        "beta_aware_tp": True,
        "beta_threshold": 1.0,
        "low_beta_target_ticks": 1,
        "high_beta_target_ticks": 2,
        "stop_ticks": 2,
        "storage_key": f"set100-paper-trade-v1-flow1045-beta-top5-{FLOW1045_BETA_PAPER_START_DATE}",
    },
}


DEFAULT_PAPER_PORTFOLIO_ID = "avg30-top5flow"


def validate_paper_state(payload, portfolio):
    if not isinstance(payload, dict):
        raise ValueError("state must be an object")
    if payload.get("version") != 1:
        raise ValueError("unsupported paper state version")
    if payload.get("startDate") != portfolio["start_date"]:
        raise ValueError("paper state start date does not match")
    if not isinstance(payload.get("positions"), dict):
        raise ValueError("positions must be an object")
    if not isinstance(payload.get("orders"), list):
        raise ValueError("orders must be an array")
    if not isinstance(payload.get("equityHistory"), list):
        raise ValueError("equityHistory must be an array")
    if not isinstance(payload.get("dailyEntries"), dict):
        raise ValueError("dailyEntries must be an object")
    if not isinstance(payload.get("lastProcessedBars"), dict):
        raise ValueError("lastProcessedBars must be an object")

    initial_capital = float(payload.get("initialCapital", 0))
    if initial_capital != PAPER_INITIAL_CAPITAL:
        raise ValueError("initial capital does not match portfolio configuration")
    for key in ("cash", "realizedPnl"):
        value = float(payload.get(key, 0))
        if not np.isfinite(value):
            raise ValueError(f"{key} must be finite")

    payload["orders"] = payload["orders"][-1000:]
    payload["equityHistory"] = payload["equityHistory"][-2500:]
    encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > 2_000_000:
        raise ValueError("paper state is too large")
    return payload


def paper_render_context(portfolio_id=DEFAULT_PAPER_PORTFOLIO_ID):
    portfolio = PAPER_PORTFOLIOS.get(portfolio_id, PAPER_PORTFOLIOS[DEFAULT_PAPER_PORTFOLIO_ID])
    browser_configs = []
    for item in PAPER_PORTFOLIOS.values():
        browser_configs.append({
            **item,
            "startDate": item["start_date"],
            "initialCapital": PAPER_INITIAL_CAPITAL,
            "commissionRate": PAPER_COMMISSION_RATE,
            "topN": item["top_n"],
            "avgLowDays": item["avg_low_days"],
            "strategy": item.get("strategy", "avg_low_touch"),
            "strategyLabel": item.get("strategy_label", f"Avg Low {item['avg_low_days']}D"),
            "entryMinute": item.get("entry_minute"),
            "entryLabel": item.get("entry_label", ""),
            "positionSizing": item.get("position_sizing", "budget"),
            "tickValue": item.get("tick_value"),
            "betaAwareTp": item.get("beta_aware_tp", False),
            "betaThreshold": item.get("beta_threshold", 1.0),
            "lowBetaTargetTicks": item.get("low_beta_target_ticks", 1),
            "highBetaTargetTicks": item.get("high_beta_target_ticks", 2),
            "stopTicks": item.get("stop_ticks", 2),
            "bidField": item["bid_field"],
            "slField": item["sl_field"],
            "positionBudget": item["position_budget"],
            "maxDailyEntries": item["max_daily_entries"],
            "storageKey": item["storage_key"],
        })
    return {
        "paper_start_date": portfolio["start_date"],
        "paper_initial_capital": PAPER_INITIAL_CAPITAL,
        "paper_position_budget": portfolio["position_budget"],
        "paper_max_daily_entries": portfolio["max_daily_entries"],
        "paper_commission_rate": PAPER_COMMISSION_RATE,
        "paper_portfolio_id": portfolio["id"],
        "paper_portfolio_label": portfolio["label"],
        "paper_top_n": portfolio["top_n"],
        "paper_avg_low_days": portfolio["avg_low_days"],
        "paper_strategy_label": portfolio.get("strategy_label", f"Avg Low {portfolio['avg_low_days']}D"),
        "paper_tick_value": portfolio.get("tick_value"),
        "paper_beta_aware": portfolio.get("beta_aware_tp", False),
        "paper_portfolio_nav": list(PAPER_PORTFOLIOS.values()),
        "paper_portfolios_json": json.dumps(browser_configs, separators=(",", ":")),
    }


def enrich_follow_fields(row, sd_row, flow_in_rank=None):
    last_price = float(row.get("last_price") or 0.0)
    current_range = float(row.get("current_range") or 0.0)
    high_zone = None
    avg_high_zone = None
    upper_30_price = None
    low_zone = None
    bid_price = None
    tp_price = None
    sl_price = None
    expected_range = None
    expected_range_30 = None
    today_high_sd = 0.0
    today_last_sd = 0.0
    today_range_sd = 0.0
    range_x_30 = 0.0
    breakout_30 = False
    tick_size = get_tick_size(last_price) if last_price > 0 else 0.0

    if sd_row:
        avg_high_zone = sd_row.get("high_price")
        high_zone = sd_row.get("upper_20_price") or avg_high_zone
        upper_30_price = sd_row.get("upper_30_price")
        low_zone = sd_row.get("low_price")
        bid_price = sd_row.get("bid_30_avg")
        tp_price = sd_row.get("tp_prev_close")
        expected_range = sd_row.get("range_20_baht") or sd_row.get("range_baht")
        expected_range_30 = sd_row.get("range_30_baht")
        tick_size = sd_row.get("bid_tick_size") or sd_row.get("tick_size") or tick_size
        base_close = float(sd_row.get("last_close") or 0.0)
        sd_frac = float(sd_row.get("sd_pct") or 0.0) / 100.0
        sd_unit = base_close * sd_frac
        if sd_unit > 0:
            session_high = float(row.get("session_high") or last_price or 0.0)
            today_high_sd = (session_high - base_close) / sd_unit
            today_last_sd = (last_price - base_close) / sd_unit
            today_range_sd = current_range / sd_unit

    expected_range = float(expected_range or 0.0)
    expected_range_30 = float(expected_range_30 or 0.0)
    vol_x = current_range / expected_range if expected_range > 0 else 0.0
    range_x_30 = current_range / expected_range_30 if expected_range_30 > 0 else 0.0
    is_top_flow = flow_in_rank is not None and flow_in_rank <= FOLLOW_DEFAULT_TOP_N
    is_buy_zone_top_flow = flow_in_rank is not None and flow_in_rank <= BUY_ZONE_TOP_N
    breakout = high_zone is not None and last_price >= float(high_zone)
    breakout_30 = upper_30_price is not None and last_price >= float(upper_30_price)
    near_high = (
        high_zone is not None
        and tick_size > 0
        and last_price >= float(high_zone) - float(tick_size)
    )
    has_positive_flow = float(row.get("net_flow") or 0.0) > 0
    bid_price_float = float(bid_price or 0.0)
    tp_price_float = float(tp_price or 0.0)
    sl_price = (
        round(bid_price_float - (2 * float(tick_size or 0.0)), 2)
        if bid_price_float > 0 and tick_size > 0 else None
    )
    bid_reward_pct = (
        (tp_price_float / bid_price_float - 1.0) * 100
        if bid_price_float > 0 and tp_price_float > bid_price_float else 0.0
    )
    bid_reward_ticks = count_price_ticks(bid_price_float, tp_price_float)
    bid_setup_ok = is_buy_zone_top_flow and has_positive_flow and bid_reward_pct > 0

    if is_top_flow and has_positive_flow and vol_x >= FOLLOW_DEFAULT_VOL_X and breakout:
        signal = "FOLLOW+" if breakout_30 or range_x_30 >= 1.10 else "FOLLOW"
        signal_class = "follow"
        follow_score = 4 if signal == "FOLLOW+" else 3
    elif is_top_flow and has_positive_flow and (vol_x >= 1.00 or near_high):
        signal = "WATCH"
        signal_class = "watch"
        follow_score = 2
    elif is_top_flow:
        signal = "TOP FLOW"
        signal_class = "flow"
        follow_score = 1
    else:
        signal = "-"
        signal_class = "none"
        follow_score = 0

    return {
        "flow_in_rank": flow_in_rank,
        "high_zone": high_zone,
        "avg_high_zone": avg_high_zone,
        "upper_30_price": upper_30_price,
        "low_zone": low_zone,
        "bid_price": bid_price,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "bid_reward_pct": bid_reward_pct,
        "bid_reward_ticks": bid_reward_ticks,
        "bid_setup_ok": bid_setup_ok,
        "expected_range": expected_range,
        "expected_range_30": expected_range_30,
        "tick_size": tick_size,
        "vol_x": vol_x,
        "range_x_30": range_x_30,
        "today_high_sd": today_high_sd,
        "today_last_sd": today_last_sd,
        "today_range_sd": today_range_sd,
        "breakout": breakout,
        "breakout_30": breakout_30,
        "follow_signal": signal,
        "follow_signal_class": signal_class,
        "follow_score": follow_score,
    }


def build_flow_rows(results, sd_rows=None):
    sd_rows = sd_rows or {}
    positive_flow = sorted(
        [r for r in results if r.get("net_flow", 0) > 0],
        key=lambda r: r.get("net_flow", 0),
        reverse=True,
    )
    flow_rank = {r["ticker"]: index + 1 for index, r in enumerate(positive_flow)}

    rows = []
    for r in results:
        follow_fields = enrich_follow_fields(
            r,
            sd_rows.get(r["ticker"]),
            flow_rank.get(r["ticker"]),
        )
        rows.append({
            **r,
            **follow_fields,
            "pct_in_round": round(r["pct_in"], 1),
            "pct_out_round": round(r["pct_out"], 1),
            "volume_k": r["volume"] / 1000.0,
            "volume_k_str": f"{r['volume'] / 1000.0:,.0f}",
        })

    closest_candidates = []
    for row in rows:
        last_price = float(row.get("last_price") or 0.0)
        bid_price = float(row.get("bid_price") or 0.0)
        distance_pct = (
            abs((last_price / bid_price) - 1.0) * 100.0
            if last_price > 0 and bid_price > 0 else None
        )
        row["bid_distance_pct"] = distance_pct
        row["bid_focus_rank"] = None
        if row.get("bid_setup_ok") and distance_pct is not None:
            closest_candidates.append(row)

    closest_candidates.sort(
        key=lambda row: (
            row["bid_distance_pct"],
            row.get("flow_in_rank") or 9999,
            row.get("ticker") or "",
        )
    )
    for focus_rank, row in enumerate(closest_candidates[:3], start=1):
        row["bid_focus_rank"] = focus_rank
    return rows


def build_paper_scan_rows(results, sd_rows):
    previous_positive = sorted(
        [r for r in results if float(r.get("prev_net_flow") or 0.0) > 0],
        key=lambda r: float(r.get("prev_net_flow") or 0.0),
        reverse=True,
    )
    previous_rank = {
        row["ticker"]: index + 1 for index, row in enumerate(previous_positive)
    }
    rows = []
    for result in results:
        sd_row = sd_rows.get(result["ticker"])
        latest_bar_close = float(result.get("latest_bar_close") or result.get("last_price") or 0.0)
        instant_bid_price = price_before_ticks(latest_bar_close, 1) if latest_bar_close > 0 else None
        instant_tp_price = price_after_ticks(instant_bid_price, 2) if instant_bid_price else None
        instant_sl_price = price_before_ticks(instant_bid_price, 2) if instant_bid_price else None
        fields = enrich_follow_fields(
            result,
            sd_row,
            previous_rank.get(result["ticker"]),
        )
        rows.append({
            "ticker": result["ticker"],
            "session_date": result.get("session_date"),
            "market_ts": result.get("market_ts"),
            "last_price": result.get("last_price"),
            "latest_bar_open": result.get("latest_bar_open"),
            "latest_bar_high": result.get("latest_bar_high"),
            "latest_bar_low": result.get("latest_bar_low"),
            "latest_bar_close": result.get("latest_bar_close"),
            "morning_exit_price": result.get("morning_exit_price"),
            "afternoon_exit_price": result.get("afternoon_exit_price"),
            "prev_net_flow": result.get("prev_net_flow"),
            "flow_rank": previous_rank.get(result["ticker"]),
            "bid_price": fields.get("bid_price"),
            "bid_price_30": fields.get("bid_price"),
            "bid_price_12": sd_row.get("bid_12_avg") if sd_row else None,
            "tp_price": fields.get("tp_price"),
            "sl_price": fields.get("sl_price"),
            "sl_price_30": fields.get("sl_price"),
            "sl_price_12": (
                round(
                    float(sd_row.get("bid_12_avg"))
                    - (2 * float(sd_row.get("bid_tick_size_12"))),
                    2,
                )
                if sd_row and sd_row.get("bid_12_avg") and sd_row.get("bid_tick_size_12")
                else None
            ),
            "instant_bid_price": instant_bid_price,
            "instant_tp_price": instant_tp_price,
            "instant_sl_price": instant_sl_price,
            "tick_size": fields.get("tick_size"),
            "paper_beta_60d": sd_row.get("paper_beta_60d") if sd_row else None,
            "paper_level_date": sd_row.get("paper_level_date") if sd_row else None,
        })
    return rows


def sort_results_like_flow(results, flow_results):
    if not flow_results:
        return results

    flow_order = {r["ticker"]: i for i, r in enumerate(flow_results)}
    fallback_offset = len(flow_order)
    return sorted(
        enumerate(results),
        key=lambda item: (flow_order.get(item[1]["ticker"], fallback_offset + item[0])),
    )


def render_dashboard(view="flow", paper_portfolio_id=DEFAULT_PAPER_PORTFOLIO_ID):
    paper_context = paper_render_context(paper_portfolio_id)
    if view == "paper":
        return render_template_string(
            PAGE, rows=[], errors=[], ts="-", months=LOOKBACK_MONTHS,
            n_ok=0, n_err=0, flow_rows=[], flow_errors=[], flow_ts="-",
            flow_n_ok=0, flow_n_err=0, performance_rows=[],
            performance_errors=[], sector_performance_rows=[], performance_ts="-",
            performance_n_ok=0, performance_n_err=0, session_label=None,
            session_is_today=True, view=view,
            page_title=f"SET100 - Paper Trade - {paper_context['paper_portfolio_label']}",
            **paper_context,
        )

    if view == "performance":
        performance_rows, performance_errors, performance_ts = load_performance(force=False)
        sector_performance_rows = build_sector_performance_rows(performance_rows)
        results = []
        errors = []
        ts = "-"
        rows = []
        flow_results = []
        flow_errors = []
        flow_ts = "-"
        flow_rows = []
        session_label = None
        session_is_today = True
        page_title = "SET100 — Performance"
        return render_template_string(
            PAGE, rows=rows, errors=errors, ts=ts,
            months=LOOKBACK_MONTHS, n_ok=0, n_err=0,
            flow_rows=flow_rows, flow_errors=flow_errors, flow_ts=flow_ts,
            flow_n_ok=0, flow_n_err=0,
            performance_rows=performance_rows, performance_errors=performance_errors,
            sector_performance_rows=sector_performance_rows,
            performance_ts=performance_ts,
            performance_n_ok=len(performance_rows), performance_n_err=len(performance_errors),
            session_label=session_label, session_is_today=session_is_today,
            view=view, page_title=page_title,
            **paper_context,
        )

    results, errors, ts = load_all(force=False)

    sd_lookup = {r["ticker"]: r.get("paper_prev_close", r["last_close"]) for r in results}

    if view in ("flow", "sd") or _flow_cache["data"] is not None:
        flow_results, flow_errors, flow_ts, session_label, session_is_today = load_all_flow(
            force=False, sd_lookup=sd_lookup
        )
    else:
        flow_results = []
        flow_errors = []
        flow_ts = "-"
        session_label = None
        session_is_today = True

    if view == "sd":
        results = [item[1] for item in sort_results_like_flow(results, flow_results)]

    rows = build_rows(results)
    sd_rows = {r["ticker"]: r for r in rows}
    flow_rows = build_flow_rows(flow_results, sd_rows)
    if view == "flow":
        flow_rows = [
            row for row in flow_rows
            if row.get("flow_in_rank") is not None and row["flow_in_rank"] <= 10
        ]
    page_title = (
        "SET100 — Money Flow TOP10"
        if view == "flow"
        else "SET100 — Daily SD Range Dashboard"
    )

    return render_template_string(
        PAGE, rows=rows, errors=errors, ts=ts,
        months=LOOKBACK_MONTHS, n_ok=len(results), n_err=len(errors),
        flow_rows=flow_rows, flow_errors=flow_errors, flow_ts=flow_ts,
        flow_n_ok=len(flow_results), flow_n_err=len(flow_errors),
        performance_rows=[], sector_performance_rows=[], performance_errors=[], performance_ts="-",
        performance_n_ok=0, performance_n_err=0,
        session_label=session_label, session_is_today=session_is_today,
        flow_market_ts=_flow_cache.get("market_ts"),
        flow_refresh_seconds=_flow_cache.get("refresh_seconds"),
        view=view, page_title=page_title,
        **paper_context,
    )


@app.route("/")
def index():
    return redirect(url_for("flow_page"))


@app.route("/health")
def health():
    flow_epoch = _flow_cache.get("epoch")
    return jsonify({
        "ok": True,
        "ts": now_th_str(),
        "flow_cache_age": round(time.time() - flow_epoch, 1) if flow_epoch else None,
        "session_label": _flow_cache.get("session_label"),
        "session_is_today": _flow_cache.get("session_is_today"),
        "market_ts": _flow_cache.get("market_ts"),
        "flow_refresh_seconds": _flow_cache.get("refresh_seconds"),
        "paper_storage": paper_state_store.backend,
        "paper_storage_persistent": paper_state_store.persistent,
    })


@app.route("/warmup")
def warmup():
    results, errors, ts = load_all(force=False)
    sd_lookup = {r["ticker"]: r.get("paper_prev_close", r["last_close"]) for r in results}
    flow_results, flow_errors, flow_ts, session_label, session_is_today = load_all_flow(
        force=True,
        sd_lookup=sd_lookup,
    )
    return jsonify({
        "ok": True,
        "ts": flow_ts,
        "sd_ts": ts,
        "session_label": session_label,
        "session_is_today": session_is_today,
        "rows": len(flow_results),
        "errors": len(errors),
        "flow_errors": len(flow_errors),
    })


@app.route("/flow")
def flow_page():
    return render_dashboard("flow")


@app.route("/sd")
def sd_page():
    return redirect(url_for("flow_page"))


@app.route("/performance")
def performance_page():
    return redirect(url_for("flow_page"))


@app.route("/paper")
def paper_page():
    return redirect(url_for("paper_portfolio_page", portfolio_id=DEFAULT_PAPER_PORTFOLIO_ID))


@app.route("/paper/<portfolio_id>")
def paper_portfolio_page(portfolio_id):
    if portfolio_id not in PAPER_PORTFOLIOS:
        return redirect(url_for("paper_portfolio_page", portfolio_id=DEFAULT_PAPER_PORTFOLIO_ID))
    return render_dashboard("paper", paper_portfolio_id=portfolio_id)


@app.route("/api/quote/<symbol>")
def api_quote(symbol):
    clean_symbol = "".join(ch for ch in symbol.upper() if ch.isalnum())
    if clean_symbol not in TICKERS:
        return jsonify({"error": "unknown ticker"}), 404
    try:
        return jsonify(get_latest_quote(clean_symbol))
    except Exception as e:
        return jsonify({"error": str(e), "ticker": clean_symbol}), 502


@app.route("/api/follow_scan")
def api_follow_scan():
    try:
        warmup_mode = request.args.get("warmup") == "1"
        if warmup_mode and not _flow_cache.get("session_is_today", False):
            session_available, probe_date = probe_today_session()
            if not session_available:
                return jsonify({
                    "rows": [],
                    "ts": _flow_cache.get("ts"),
                    "session_label": probe_date or _flow_cache.get("session_label"),
                    "session_is_today": False,
                    "retry_after_seconds": flow_probe_retry_seconds(),
                    "probe_only": True,
                    "errors": 0,
                    "flow_errors": len(_flow_cache.get("errors") or []),
                })

        results, errors, ts = load_all(force=False)
        sd_lookup = {r["ticker"]: r.get("paper_prev_close", r["last_close"]) for r in results}
        rows = build_rows(results)
        sd_rows = {r["ticker"]: r for r in rows}
        flow_is_stale = warmup_mode or time.time() - _flow_cache.get("epoch", 0) > 45
        flow_results, flow_errors, flow_ts, session_label, session_is_today = load_all_flow(
            force=flow_is_stale,
            sd_lookup=sd_lookup,
        )
        flow_rows = build_flow_rows(flow_results, sd_rows)
        compact_rows = []
        for row in flow_rows:
            compact_rows.append({
                "ticker": row["ticker"],
                "last_price": row.get("last_price"),
                "net_flow": row.get("net_flow"),
                "flow_in_rank": row.get("flow_in_rank"),
                "vol_x": row.get("vol_x"),
                "range_x_30": row.get("range_x_30"),
                "high_zone": row.get("high_zone"),
                "upper_30_price": row.get("upper_30_price"),
                "today_high_sd": row.get("today_high_sd"),
                "tick_size": row.get("tick_size"),
                "current_range": row.get("current_range"),
                "breakout": row.get("breakout"),
                "breakout_30": row.get("breakout_30"),
                "follow_signal": row.get("follow_signal"),
                "follow_score": row.get("follow_score"),
                "session_date": row.get("session_date"),
            })
        return jsonify({
            "rows": compact_rows,
            "ts": flow_ts,
            "sd_ts": ts,
            "session_label": session_label,
            "session_is_today": session_is_today,
            "market_ts": _flow_cache.get("market_ts"),
            "refresh_seconds": _flow_cache.get("refresh_seconds"),
            "retry_after_seconds": flow_probe_retry_seconds(),
            "errors": len(errors),
            "flow_errors": len(flow_errors),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/paper_scan")
def api_paper_scan():
    try:
        results, errors, sd_ts = load_all(force=False)
        sd_lookup = {
            row["ticker"]: row.get("paper_prev_close", row["last_close"])
            for row in results
        }
        sd_rows_list = build_rows(results)
        sd_rows = {row["ticker"]: row for row in sd_rows_list}
        flow_is_stale = time.time() - _flow_cache.get("epoch", 0) > 55
        flow_results, flow_errors, flow_ts, session_label, session_is_today = load_all_flow(
            force=flow_is_stale,
            sd_lookup=sd_lookup,
        )
        rows = build_paper_scan_rows(flow_results, sd_rows)
        return jsonify({
            "rows": rows,
            "ts": flow_ts,
            "sd_ts": sd_ts,
            "session_label": session_label,
            "session_is_today": session_is_today,
            "start_date": PAPER_START_DATE,
            "initial_capital": PAPER_INITIAL_CAPITAL,
            "position_budget": PAPER_POSITION_BUDGET,
            "max_daily_entries": PAPER_MAX_DAILY_ENTRIES,
            "commission_rate": PAPER_COMMISSION_RATE,
            "errors": len(errors),
            "flow_errors": len(flow_errors),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/paper_state/<portfolio_id>", methods=["GET", "PUT", "DELETE"])
def api_paper_state(portfolio_id):
    portfolio = PAPER_PORTFOLIOS.get(portfolio_id)
    if not portfolio:
        return jsonify({"error": "unknown paper portfolio"}), 404

    try:
        if request.method == "GET":
            saved = paper_state_store.get(portfolio_id)
            response = {
                "exists": saved is not None,
                "portfolio_id": portfolio_id,
                "storage": paper_state_store.backend,
                "persistent": paper_state_store.persistent,
            }
            if saved:
                response.update(saved)
            result = jsonify(response)
            result.headers["Cache-Control"] = "no-store"
            return result

        if request.method == "DELETE":
            paper_state_store.delete(portfolio_id)
            return jsonify({
                "ok": True,
                "portfolio_id": portfolio_id,
                "storage": paper_state_store.backend,
                "persistent": paper_state_store.persistent,
            })

        if request.content_length and request.content_length > 2_100_000:
            return jsonify({"error": "paper state is too large"}), 413
        body = request.get_json(silent=True) or {}
        state = validate_paper_state(body.get("state"), portfolio)
        base_revision = body.get("baseRevision")
        if base_revision is not None:
            base_revision = int(base_revision)
        saved = paper_state_store.put(
            portfolio_id,
            state,
            expected_revision=base_revision,
            allow_richer_migration=body.get("migration") is True,
        )
        return jsonify({
            "ok": True,
            "portfolio_id": portfolio_id,
            "storage": paper_state_store.backend,
            "persistent": paper_state_store.persistent,
            **saved,
        })
    except PaperStateConflict as conflict:
        return jsonify({
            "error": "paper state conflict",
            "state": conflict.state,
            "revision": conflict.revision,
            "storage": paper_state_store.backend,
            "persistent": paper_state_store.persistent,
        }), 409
    except (TypeError, ValueError) as error:
        return jsonify({"error": str(error)}), 400
    except Exception as error:
        app.logger.exception("paper state storage failed")
        return jsonify({"error": str(error)}), 503


@app.route("/refresh")
def refresh():
    load_all(force=True)
    return redirect(url_for("flow_page"))


@app.route("/refresh_flow")
def refresh_flow():
    results, _, _ = load_all(force=False)
    sd_lookup = {r["ticker"]: r.get("paper_prev_close", r["last_close"]) for r in results}
    load_all_flow(force=True, sd_lookup=sd_lookup)
    return redirect(url_for("flow_page"))


@app.route("/refresh_performance")
def refresh_performance():
    load_performance(force=True)
    return redirect(url_for("performance_page"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5001"))
    app.run(host="0.0.0.0", debug=False, port=port)
