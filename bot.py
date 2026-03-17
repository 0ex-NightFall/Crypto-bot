import os
import json
import logging
import asyncio
import math
from datetime import datetime, date
from typing import Dict, List, Optional
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters, ConversationHandler,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ============================================================
# KONFIGURASI
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ISI_TOKEN_BOT_KAMU_DISINI")
COINGECKO_API = "https://api.coingecko.com/api/v3"
DATA_FILE = "data.json"

# Conversation states
(WAITING_COIN, WAITING_PRICE, WAITING_DIRECTION) = range(3)
(WAITING_TRADE_COIN, WAITING_TRADE_TYPE, WAITING_TRADE_PRICE,
 WAITING_TRADE_AMOUNT, WAITING_TRADE_NOTE) = range(5, 10)
(WAITING_PORT_COIN, WAITING_PORT_AMOUNT, WAITING_PORT_BUY_PRICE) = range(10, 13)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# DATA MANAGEMENT
# ============================================================
def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"alerts": {}, "trades": {}, "watchlist": {}, "portfolio": {}}

def save_data(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ============================================================
# API FUNCTIONS
# ============================================================
async def get_price(coin_id: str) -> Optional[dict]:
    url = f"{COINGECKO_API}/simple/price"
    params = {
        "ids": coin_id, "vs_currencies": "usd,idr",
        "include_24hr_change": "true", "include_market_cap": "true",
        "include_24hr_vol": "true"
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    d = await r.json()
                    return d.get(coin_id)
    except Exception as e:
        logger.error(f"Price error {coin_id}: {e}")
    return None

async def search_coin(query: str) -> List[dict]:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{COINGECKO_API}/search", params={"query": query},
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return (await r.json()).get("coins", [])[:5]
    except Exception as e:
        logger.error(f"Search error: {e}")
    return []

async def get_ohlc(coin_id: str, days: int = 7) -> Optional[List]:
    """Ambil data OHLC untuk chart candlestick."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{COINGECKO_API}/coins/{coin_id}/ohlc",
                params={"vs_currency": "usd", "days": str(days)},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                if r.status == 200:
                    return await r.json()
    except Exception as e:
        logger.error(f"OHLC error: {e}")
    return None

async def get_market_chart(coin_id: str, days: int = 30) -> Optional[dict]:
    """Ambil data harga historis untuk kalkulasi indikator."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{COINGECKO_API}/coins/{coin_id}/market_chart",
                params={"vs_currency": "usd", "days": str(days), "interval": "daily"},
                timeout=aiohttp.ClientTimeout(total=15)
            ) as r:
                if r.status == 200:
                    return await r.json()
    except Exception as e:
        logger.error(f"Chart error: {e}")
    return None

async def get_trending() -> List[dict]:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{COINGECKO_API}/search/trending",
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return (await r.json()).get("coins", [])
    except:
        pass
    return []

# ============================================================
# FORMATTERS
# ============================================================
def fmt_price(p: float) -> str:
    if p >= 1: return f"${p:,.2f}"
    elif p >= 0.01: return f"${p:.4f}"
    return f"${p:.8f}"

def fmt_idr(p: float) -> str:
    return f"Rp {p:,.0f}"

def fmt_change(c: float) -> str:
    e = "🟢" if c >= 0 else "🔴"
    return f"{e} {'+' if c>=0 else ''}{c:.2f}%"

def fmt_large(n: float) -> str:
    if n >= 1e9: return f"${n/1e9:.2f}B"
    if n >= 1e6: return f"${n/1e6:.2f}M"
    if n >= 1e3: return f"${n/1e3:.2f}K"
    return f"${n:.2f}"

# ============================================================
# TECHNICAL INDICATORS
# ============================================================
def calc_rsi(prices: List[float], period: int = 14) -> Optional[float]:
    """Hitung RSI (Relative Strength Index)."""
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_sma(prices: List[float], period: int) -> Optional[float]:
    """Hitung Simple Moving Average."""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period

def calc_ema(prices: List[float], period: int) -> Optional[float]:
    """Hitung Exponential Moving Average."""
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_macd(prices: List[float]):
    """Hitung MACD (12, 26, 9)."""
    if len(prices) < 26:
        return None, None, None
    ema12 = calc_ema(prices, 12)
    ema26 = calc_ema(prices, 26)
    if ema12 is None or ema26 is None:
        return None, None, None
    macd_line = ema12 - ema26
    # Signal line: EMA 9 dari MACD (simplified)
    macd_values = []
    for i in range(26, len(prices)+1):
        e12 = calc_ema(prices[:i], 12)
        e26 = calc_ema(prices[:i], 26)
        if e12 and e26:
            macd_values.append(e12 - e26)
    signal = calc_ema(macd_values, 9) if len(macd_values) >= 9 else None
    histogram = macd_line - signal if signal else None
    return macd_line, signal, histogram

def calc_bollinger(prices: List[float], period: int = 20):
    """Hitung Bollinger Bands."""
    if len(prices) < period:
        return None, None, None
    recent = prices[-period:]
    sma = sum(recent) / period
    variance = sum((p - sma) ** 2 for p in recent) / period
    std = math.sqrt(variance)
    return sma + 2*std, sma, sma - 2*std  # upper, middle, lower

def get_signal_summary(rsi, macd, macd_signal, price, sma20, sma50, bb_upper, bb_lower) -> dict:
    """Buat ringkasan sinyal trading."""
    signals = []
    score = 0  # positif = bullish, negatif = bearish

    # RSI
    if rsi is not None:
        if rsi < 30:
            signals.append(("🟢", "RSI Oversold", f"RSI={rsi:.1f} → potensi naik"))
            score += 2
        elif rsi > 70:
            signals.append(("🔴", "RSI Overbought", f"RSI={rsi:.1f} → potensi turun"))
            score -= 2
        elif 40 <= rsi <= 60:
            signals.append(("⚪", "RSI Netral", f"RSI={rsi:.1f} → sideways"))
        else:
            signals.append(("🟡", "RSI Normal", f"RSI={rsi:.1f}"))

    # MACD
    if macd is not None and macd_signal is not None:
        if macd > macd_signal:
            signals.append(("🟢", "MACD Bullish", "MACD di atas signal → momentum naik"))
            score += 1
        else:
            signals.append(("🔴", "MACD Bearish", "MACD di bawah signal → momentum turun"))
            score -= 1

    # Moving Average
    if sma20 and sma50 and price:
        if price > sma20 > sma50:
            signals.append(("🟢", "MA Bullish", f"Harga > SMA20 > SMA50"))
            score += 2
        elif price < sma20 < sma50:
            signals.append(("🔴", "MA Bearish", f"Harga < SMA20 < SMA50"))
            score -= 2
        elif price > sma20:
            signals.append(("🟡", "MA Mixed", "Harga di atas SMA20"))
            score += 1

    # Bollinger Bands
    if bb_upper and bb_lower and price:
        if price >= bb_upper:
            signals.append(("🔴", "BB Overbought", "Harga menyentuh upper band"))
            score -= 1
        elif price <= bb_lower:
            signals.append(("🟢", "BB Oversold", "Harga menyentuh lower band"))
            score += 1

    # Overall
    if score >= 3:
        overall = ("🚀", "STRONG BUY", "Mayoritas indikator bullish")
    elif score >= 1:
        overall = ("🟢", "BUY", "Lebih banyak sinyal bullish")
    elif score == 0:
        overall = ("⚪", "NETRAL", "Sinyal campur, tunggu konfirmasi")
    elif score >= -2:
        overall = ("🔴", "SELL", "Lebih banyak sinyal bearish")
    else:
        overall = ("💀", "STRONG SELL", "Mayoritas indikator bearish")

    return {"signals": signals, "overall": overall, "score": score}

# ============================================================
# ASCII CHART GENERATOR
# ============================================================
def make_ascii_chart(prices: List[float], width: int = 30, height: int = 10) -> str:
    """Buat chart ASCII sederhana dari list harga."""
    if len(prices) < 2:
        return "Data tidak cukup"
    prices = prices[-width:]
    min_p = min(prices)
    max_p = max(prices)
    if max_p == min_p:
        return "Harga flat"
    rows = []
    for row in range(height, -1, -1):
        line = ""
        threshold = min_p + (max_p - min_p) * row / height
        for i, p in enumerate(prices):
            if i == 0:
                line += "│"
                continue
            prev = prices[i-1]
            if min(p, prev) <= threshold <= max(p, prev):
                line += "╱" if p > prev else ("╲" if p < prev else "─")
            elif p >= threshold and prev >= threshold:
                line += "─"
            else:
                line += " "
        rows.append(line)
    rows.append("└" + "─" * (len(prices)))
    return "\n".join(rows)

# ============================================================
# COMMAND: /start
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = f"""
🤖 *Halo, {user.first_name}!* Selamat datang di *CryptoBot Pro* 🚀

*📋 Semua Fitur:*

📊 *Harga & Analisis*
/harga `<koin>` — Harga real-time
/grafik `<koin>` — Chart harga 7 hari
/sinyal `<koin>` — Sinyal trading (RSI, MACD, MA, BB)
/trending — Crypto trending

🔔 *Alert Harga*
/alert — Pasang alert otomatis
/listalert — Lihat alert aktif
/hapusalert — Hapus alert

💼 *Portfolio*
/portfolio — Lihat portfolio & P&L
/tambah\_aset — Tambah aset ke portfolio
/hapus\_aset — Hapus aset dari portfolio

📒 *Trade Journal*
/tambah\_trade — Catat trade baru
/tradesaya — Riwayat trade
/rekap — Rekap harian
/rekapbulanan — Rekap bulanan

/bantuan — Panduan lengkap
"""
    keyboard = [
        [InlineKeyboardButton("📊 Harga", callback_data="menu_harga"),
         InlineKeyboardButton("📈 Grafik", callback_data="menu_grafik"),
         InlineKeyboardButton("🤖 Sinyal", callback_data="menu_sinyal")],
        [InlineKeyboardButton("💼 Portfolio", callback_data="menu_portfolio"),
         InlineKeyboardButton("🔔 Alert", callback_data="menu_alert")],
        [InlineKeyboardButton("📒 Catat Trade", callback_data="menu_trade")]
    ]
    await update.message.reply_text(text, parse_mode="Markdown",
                                     reply_markup=InlineKeyboardMarkup(keyboard))

# ============================================================
# COMMAND: /harga
# ============================================================
async def cek_harga(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Contoh: `/harga bitcoin`", parse_mode="Markdown")
        return
    query = " ".join(context.args).lower()
    msg = await update.message.reply_text("⏳ Mengambil data...")
    coins = await search_coin(query)
    if not coins:
        await msg.edit_text(f"❌ Koin *{query}* tidak ditemukan.", parse_mode="Markdown")
        return
    coin = coins[0]
    price_data = await get_price(coin["id"])
    if not price_data:
        await msg.edit_text("❌ Gagal mengambil harga.")
        return
    usd = price_data.get("usd", 0)
    idr = price_data.get("idr", 0)
    change = price_data.get("usd_24h_change", 0)
    mcap = price_data.get("usd_market_cap", 0)
    vol = price_data.get("usd_24h_vol", 0)
    sym = coin["symbol"].upper()
    text = f"""
💰 *{coin['name']} ({sym})*

💵 USD: *{fmt_price(usd)}*
🇮🇩 IDR: *{fmt_idr(idr)}*
📈 24j: *{fmt_change(change)}*
💹 Market Cap: *{fmt_large(mcap)}*
📊 Volume 24j: *{fmt_large(vol)}*

🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}
"""
    keyboard = [
        [InlineKeyboardButton("📈 Grafik 7H", callback_data=f"grafik_{coin['id']}_{sym}"),
         InlineKeyboardButton("🤖 Sinyal", callback_data=f"sinyal_{coin['id']}_{sym}")],
        [InlineKeyboardButton("🔔 Alert", callback_data=f"alert_{coin['id']}_{coin['name']}_{sym}"),
         InlineKeyboardButton("💼 + Portfolio", callback_data=f"port_add_{coin['id']}_{coin['name']}_{sym}")]
    ]
    await msg.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

# ============================================================
# COMMAND: /grafik
# ============================================================
async def grafik_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Contoh: `/grafik bitcoin`", parse_mode="Markdown")
        return
    query = " ".join(context.args).lower()
    msg = await update.message.reply_text("⏳ Memuat grafik...")
    coins = await search_coin(query)
    if not coins:
        await msg.edit_text("❌ Koin tidak ditemukan.")
        return
    coin = coins[0]
    await _show_grafik(msg, coin["id"], coin["name"], coin["symbol"].upper())

async def _show_grafik(msg, coin_id: str, coin_name: str, coin_symbol: str, days: int = 7):
    chart_data = await get_market_chart(coin_id, days)
    if not chart_data:
        await msg.edit_text("❌ Gagal mengambil data grafik.")
        return

    prices = [p[1] for p in chart_data.get("prices", [])]
    if len(prices) < 3:
        await msg.edit_text("❌ Data tidak cukup untuk grafik.")
        return

    # Hitung statistik
    current = prices[-1]
    start_p = prices[0]
    high = max(prices)
    low = min(prices)
    change_pct = ((current - start_p) / start_p) * 100
    change_emoji = "📈" if change_pct >= 0 else "📉"

    # Buat ASCII chart
    chart = make_ascii_chart(prices, width=28, height=8)

    # Label waktu
    label = {7: "7 Hari", 14: "14 Hari", 30: "30 Hari"}.get(days, f"{days} Hari")

    text = f"""
{change_emoji} *{coin_name} ({coin_symbol}) — {label}*

```
{chart}
```
📊 *Statistik {label}:*
• Tertinggi: *{fmt_price(high)}*
• Terendah: *{fmt_price(low)}*
• Sekarang: *{fmt_price(current)}*
• Perubahan: *{'+' if change_pct>=0 else ''}{change_pct:.2f}%*
"""
    keyboard = [
        [InlineKeyboardButton("7H", callback_data=f"chart_7_{coin_id}_{coin_symbol}"),
         InlineKeyboardButton("14H", callback_data=f"chart_14_{coin_id}_{coin_symbol}"),
         InlineKeyboardButton("30H", callback_data=f"chart_30_{coin_id}_{coin_symbol}")],
        [InlineKeyboardButton("🤖 Lihat Sinyal", callback_data=f"sinyal_{coin_id}_{coin_symbol}")]
    ]
    await msg.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

# ============================================================
# COMMAND: /sinyal
# ============================================================
async def sinyal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Contoh: `/sinyal bitcoin`", parse_mode="Markdown")
        return
    query = " ".join(context.args).lower()
    msg = await update.message.reply_text("⏳ Menganalisis indikator teknikal...")
    coins = await search_coin(query)
    if not coins:
        await msg.edit_text("❌ Koin tidak ditemukan.")
        return
    coin = coins[0]
    await _show_sinyal(msg, coin["id"], coin["name"], coin["symbol"].upper())

async def _show_sinyal(msg, coin_id: str, coin_name: str, coin_symbol: str):
    # Ambil data 60 hari untuk kalkulasi indikator
    chart_data = await get_market_chart(coin_id, 60)
    price_data = await get_price(coin_id)

    if not chart_data or not price_data:
        await msg.edit_text("❌ Gagal mengambil data analisis.")
        return

    prices = [p[1] for p in chart_data.get("prices", [])]
    current_price = price_data.get("usd", 0)

    if len(prices) < 26:
        await msg.edit_text("❌ Data historis tidak cukup untuk analisis.")
        return

    # Hitung semua indikator
    rsi = calc_rsi(prices)
    sma20 = calc_sma(prices, 20)
    sma50 = calc_sma(prices, 50)
    ema12 = calc_ema(prices, 12)
    ema26 = calc_ema(prices, 26)
    macd_val, macd_sig, macd_hist = calc_macd(prices)
    bb_upper, bb_mid, bb_lower = calc_bollinger(prices)

    # Analisis sinyal
    result = get_signal_summary(rsi, macd_val, macd_sig, current_price,
                                 sma20, sma50, bb_upper, bb_lower)
    overall_emoji, overall_text, overall_desc = result["overall"]

    # Format output
    text = f"""
🤖 *SINYAL TRADING — {coin_name} ({coin_symbol})*

💵 Harga: *{fmt_price(current_price)}*

━━━━━━━━━━━━━━
*📊 INDIKATOR TEKNIKAL:*
"""
    # RSI
    if rsi is not None:
        rsi_zone = "Oversold 🟢" if rsi < 30 else ("Overbought 🔴" if rsi > 70 else "Normal ⚪")
        text += f"• RSI(14): *{rsi:.1f}* — {rsi_zone}\n"

    # Moving Averages
    if sma20: text += f"• SMA 20: *{fmt_price(sma20)}*\n"
    if sma50: text += f"• SMA 50: *{fmt_price(sma50)}*\n"
    if ema12: text += f"• EMA 12: *{fmt_price(ema12)}*\n"
    if ema26: text += f"• EMA 26: *{fmt_price(ema26)}*\n"

    # MACD
    if macd_val is not None:
        macd_dir = "Bullish 🟢" if macd_val > (macd_sig or 0) else "Bearish 🔴"
        text += f"• MACD: *{macd_val:.4f}* — {macd_dir}\n"

    # Bollinger Bands
    if bb_upper and bb_lower:
        text += f"• BB Upper: *{fmt_price(bb_upper)}*\n"
        text += f"• BB Lower: *{fmt_price(bb_lower)}*\n"

    text += f"""
━━━━━━━━━━━━━━
*🔍 DETAIL SINYAL:*
"""
    for emoji, title, desc in result["signals"]:
        text += f"{emoji} *{title}*: {desc}\n"

    text += f"""
━━━━━━━━━━━━━━
*⚡ KESIMPULAN:*
{overall_emoji} *{overall_text}*
_{overall_desc}_

⚠️ _Ini bukan saran finansial. Selalu DYOR!_
🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}
"""
    keyboard = [
        [InlineKeyboardButton("🔔 Pasang Alert", callback_data=f"alert_{coin_id}_{coin_name}_{coin_symbol}"),
         InlineKeyboardButton("📈 Lihat Grafik", callback_data=f"grafik_{coin_id}_{coin_symbol}")]
    ]
    await msg.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

# ============================================================
# COMMAND: /portfolio
# ============================================================
async def portfolio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()
    port = data.get("portfolio", {}).get(user_id, {})

    if not port:
        text = """💼 *Portfolio kamu kosong!*

Tambahkan aset dengan:
/tambah\_aset — Masukkan koin, jumlah, dan harga beli"""
        await update.message.reply_text(text, parse_mode="Markdown")
        return

    msg = await update.message.reply_text("⏳ Menghitung portfolio...")

    total_invested = 0.0
    total_current = 0.0
    rows = []

    for coin_id, info in port.items():
        price_data = await get_price(coin_id)
        if not price_data:
            rows.append(f"• *{info['symbol']}*: ❌ Error\n")
            continue
        current_price = price_data.get("usd", 0)
        amount = info["amount"]
        buy_price = info["buy_price"]

        invested = amount * buy_price
        current_val = amount * current_price
        pnl = current_val - invested
        pnl_pct = ((current_val - invested) / invested * 100) if invested > 0 else 0
        pnl_e = "🟢" if pnl >= 0 else "🔴"

        total_invested += invested
        total_current += current_val

        rows.append(
            f"*{info['name']}* ({info['symbol']})\n"
            f"  📦 {amount} × {fmt_price(current_price)}\n"
            f"  💰 Nilai: *{fmt_price(current_val)}*\n"
            f"  {pnl_e} P&L: *{fmt_price(pnl)}* ({'+' if pnl_pct>=0 else ''}{pnl_pct:.1f}%)\n"
        )

    total_pnl = total_current - total_invested
    total_pnl_pct = ((total_current - total_invested) / total_invested * 100) if total_invested > 0 else 0
    total_e = "🟢" if total_pnl >= 0 else "🔴"

    text = "💼 *PORTFOLIO KAMU*\n\n"
    text += "\n".join(rows)
    text += f"""
━━━━━━━━━━━━━━
💵 Total Invested: *{fmt_price(total_invested)}*
📊 Nilai Sekarang: *{fmt_price(total_current)}*
{total_e} Total P&L: *{fmt_price(total_pnl)}* ({'+' if total_pnl_pct>=0 else ''}{total_pnl_pct:.1f}%)

🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}
"""
    keyboard = [
        [InlineKeyboardButton("➕ Tambah Aset", callback_data="menu_tambah_aset"),
         InlineKeyboardButton("🗑️ Hapus Aset", callback_data="menu_hapus_aset")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="refresh_portfolio")]
    ]
    await msg.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

# ============================================================
# CONVERSATION: Tambah Aset Portfolio
# ============================================================
async def tambah_aset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💼 *TAMBAH ASET KE PORTFOLIO*\n\nMasukkan nama koin:\n(contoh: `bitcoin`, `ethereum`)\n\n/batal untuk batalkan.",
        parse_mode="Markdown"
    )
    return WAITING_PORT_COIN

async def port_coin_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip().lower()
    msg = await update.message.reply_text("⏳ Mencari koin...")
    coins = await search_coin(query)
    if not coins:
        await msg.edit_text("❌ Koin tidak ditemukan. Coba lagi.")
        return WAITING_PORT_COIN
    coin = coins[0]
    price_data = await get_price(coin["id"])
    current = price_data.get("usd", 0) if price_data else 0
    context.user_data.update({"port_coin_id": coin["id"], "port_coin_name": coin["name"],
                               "port_coin_symbol": coin["symbol"].upper()})
    await msg.edit_text(
        f"✅ *{coin['name']}* ({coin['symbol'].upper()})\n"
        f"💵 Harga sekarang: *{fmt_price(current)}*\n\n"
        f"Masukkan *jumlah koin* yang kamu miliki:\n(contoh: `0.5` atau `100`)",
        parse_mode="Markdown"
    )
    return WAITING_PORT_AMOUNT

async def port_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip().replace(",", ""))
        if amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Jumlah tidak valid. Contoh: `0.5`", parse_mode="Markdown")
        return WAITING_PORT_AMOUNT
    context.user_data["port_amount"] = amount
    await update.message.reply_text(
        f"Jumlah: *{amount}* koin\n\nMasukkan *harga beli* dalam USD:\n(contoh: `45000`)\n\nKetik `0` untuk pakai harga sekarang.",
        parse_mode="Markdown"
    )
    return WAITING_PORT_BUY_PRICE

async def port_buy_price_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        buy_price = float(update.message.text.strip().replace(",", ""))
        if buy_price < 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Harga tidak valid.", parse_mode="Markdown")
        return WAITING_PORT_BUY_PRICE

    # Kalau 0, pakai harga sekarang
    if buy_price == 0:
        price_data = await get_price(context.user_data["port_coin_id"])
        buy_price = price_data.get("usd", 0) if price_data else 0

    user_id = str(update.effective_user.id)
    data = load_data()
    if "portfolio" not in data:
        data["portfolio"] = {}
    if user_id not in data["portfolio"]:
        data["portfolio"][user_id] = {}

    coin_id = context.user_data["port_coin_id"]
    data["portfolio"][user_id][coin_id] = {
        "name": context.user_data["port_coin_name"],
        "symbol": context.user_data["port_coin_symbol"],
        "amount": context.user_data["port_amount"],
        "buy_price": buy_price,
        "added_at": datetime.now().isoformat()
    }
    save_data(data)

    total = context.user_data["port_amount"] * buy_price
    await update.message.reply_text(
        f"✅ *Aset berhasil ditambahkan!*\n\n"
        f"🪙 {context.user_data['port_coin_name']} ({context.user_data['port_coin_symbol']})\n"
        f"📦 Jumlah: {context.user_data['port_amount']} koin\n"
        f"💵 Harga Beli: {fmt_price(buy_price)}\n"
        f"💰 Total Invested: {fmt_price(total)}\n\n"
        f"Ketik /portfolio untuk lihat semua aset kamu!",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def hapus_aset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()
    port = data.get("portfolio", {}).get(user_id, {})
    if not port:
        await update.message.reply_text("💼 Portfolio kamu sudah kosong.")
        return
    keyboard = []
    for coin_id, info in port.items():
        keyboard.append([InlineKeyboardButton(
            f"🗑️ {info['name']} ({info['symbol']})",
            callback_data=f"del_asset_{coin_id}"
        )])
    keyboard.append([InlineKeyboardButton("❌ Batal", callback_data="batal")])
    await update.message.reply_text(
        "Pilih aset yang ingin dihapus:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ============================================================
# ALERT SYSTEM
# ============================================================
async def alert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔔 *PASANG ALERT HARGA*\n\nMasukkan nama koin:\n\n/batal untuk batalkan.",
        parse_mode="Markdown"
    )
    return WAITING_COIN

async def alert_coin_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip().lower()
    msg = await update.message.reply_text("⏳ Mencari...")
    coins = await search_coin(query)
    if not coins:
        await msg.edit_text("❌ Tidak ditemukan. Coba lagi.")
        return WAITING_COIN
    coin = coins[0]
    price_data = await get_price(coin["id"])
    current = price_data.get("usd", 0) if price_data else 0
    context.user_data.update({"alert_coin_id": coin["id"], "alert_coin_name": coin["name"],
                               "alert_coin_symbol": coin["symbol"].upper()})
    await msg.edit_text(
        f"✅ *{coin['name']}* ({coin['symbol'].upper()})\n"
        f"💵 Harga sekarang: *{fmt_price(current)}*\n\n"
        f"Masukkan harga target (USD):\nContoh: `45000` atau `0.5`",
        parse_mode="Markdown"
    )
    return WAITING_PRICE

async def alert_price_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip().replace(",", ""))
        if price <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Harga tidak valid.", parse_mode="Markdown")
        return WAITING_PRICE
    context.user_data["alert_price"] = price
    keyboard = [
        [InlineKeyboardButton("📈 Di ATAS harga ini", callback_data="dir_above")],
        [InlineKeyboardButton("📉 Di BAWAH harga ini", callback_data="dir_below")]
    ]
    await update.message.reply_text(
        f"Target: *{fmt_price(price)}*\n\nKirim notif ketika harga:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return WAITING_DIRECTION

async def alert_direction_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    direction = "above" if query.data == "dir_above" else "below"
    user_id = str(update.effective_user.id)
    data = load_data()
    if user_id not in data["alerts"]:
        data["alerts"][user_id] = []
    data["alerts"][user_id].append({
        "id": f"{context.user_data['alert_coin_id']}_{datetime.now().timestamp():.0f}",
        "coin_id": context.user_data["alert_coin_id"],
        "coin_name": context.user_data["alert_coin_name"],
        "coin_symbol": context.user_data["alert_coin_symbol"],
        "target_price": context.user_data["alert_price"],
        "direction": direction, "triggered": False,
        "created_at": datetime.now().isoformat()
    })
    save_data(data)
    dir_text = "naik di atas" if direction == "above" else "turun di bawah"
    await query.edit_message_text(
        f"✅ *Alert dipasang!*\n\n"
        f"🪙 {context.user_data['alert_coin_name']} ({context.user_data['alert_coin_symbol']})\n"
        f"🎯 Target: *{fmt_price(context.user_data['alert_price'])}*\n"
        f"📡 Kondisi: Harga *{dir_text}* target\n\n"
        f"Notifikasi otomatis akan dikirim! 🔔",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def list_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()
    alerts = [a for a in data["alerts"].get(user_id, []) if not a.get("triggered")]
    if not alerts:
        await update.message.reply_text("📭 Belum ada alert aktif.\n\n/alert untuk pasang alert baru.")
        return
    text = f"🔔 *ALERT AKTIF* ({len(alerts)})\n\n"
    for i, a in enumerate(alerts, 1):
        d = "📈 NAIK >" if a["direction"] == "above" else "📉 TURUN <"
        text += f"{i}. *{a['coin_name']}* ({a['coin_symbol']})\n   {d} {fmt_price(a['target_price'])}\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def hapus_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()
    data["alerts"][user_id] = []
    save_data(data)
    await update.message.reply_text("🗑️ Semua alert dihapus!")

# ============================================================
# TRADE JOURNAL
# ============================================================
async def tambah_trade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📒 *CATAT TRADE BARU*\n\nMasukkan nama koin:\n\n/batal untuk batalkan.",
        parse_mode="Markdown"
    )
    return WAITING_TRADE_COIN

async def trade_coin_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Mencari...")
    coins = await search_coin(update.message.text.strip().lower())
    if not coins:
        await msg.edit_text("❌ Tidak ditemukan.")
        return WAITING_TRADE_COIN
    coin = coins[0]
    context.user_data.update({"trade_coin_id": coin["id"], "trade_coin_name": coin["name"],
                               "trade_coin_symbol": coin["symbol"].upper()})
    keyboard = [[InlineKeyboardButton("🟢 BUY", callback_data="trade_BUY")],
                [InlineKeyboardButton("🔴 SELL", callback_data="trade_SELL")]]
    await msg.edit_text(f"✅ *{coin['name']}*\n\nJenis trade:",
                         parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    return WAITING_TRADE_TYPE

async def trade_type_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["trade_type"] = query.data.split("_")[1]
    await query.edit_message_text(
        f"Tipe: *{context.user_data['trade_type']}*\n\nHarga trade (USD):",
        parse_mode="Markdown"
    )
    return WAITING_TRADE_PRICE

async def trade_price_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip().replace(",", ""))
        if price <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Harga tidak valid.", parse_mode="Markdown")
        return WAITING_TRADE_PRICE
    context.user_data["trade_price"] = price
    await update.message.reply_text(f"Harga: *{fmt_price(price)}*\n\nJumlah koin:", parse_mode="Markdown")
    return WAITING_TRADE_AMOUNT

async def trade_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip().replace(",", ""))
        if amount <= 0: raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Jumlah tidak valid.", parse_mode="Markdown")
        return WAITING_TRADE_AMOUNT
    context.user_data["trade_amount"] = amount
    total = amount * context.user_data["trade_price"]
    context.user_data["trade_total"] = total
    await update.message.reply_text(
        f"Jumlah: *{amount}* | Total: *{fmt_price(total)}*\n\nTambah catatan (atau ketik `-`):",
        parse_mode="Markdown"
    )
    return WAITING_TRADE_NOTE

async def trade_note_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    note = update.message.text.strip()
    if note == "-": note = ""
    user_id = str(update.effective_user.id)
    data = load_data()
    if user_id not in data["trades"]: data["trades"][user_id] = []
    trade = {
        "id": f"trade_{datetime.now().timestamp():.0f}",
        "coin_id": context.user_data["trade_coin_id"],
        "coin_name": context.user_data["trade_coin_name"],
        "coin_symbol": context.user_data["trade_coin_symbol"],
        "type": context.user_data["trade_type"],
        "price": context.user_data["trade_price"],
        "amount": context.user_data["trade_amount"],
        "total": context.user_data["trade_total"],
        "note": note, "date": date.today().isoformat(),
        "datetime": datetime.now().isoformat()
    }
    data["trades"][user_id].append(trade)
    save_data(data)
    emoji = "🟢" if trade["type"] == "BUY" else "🔴"
    await update.message.reply_text(
        f"✅ *Trade dicatat!*\n\n{emoji} *{trade['type']}* {trade['coin_name']}\n"
        f"💵 {fmt_price(trade['price'])} × {trade['amount']} = *{fmt_price(trade['total'])}*\n"
        f"📅 {trade['date']}" + (f"\n📝 {note}" if note else ""),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def trade_saya(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()
    trades = data["trades"].get(user_id, [])
    if not trades:
        await update.message.reply_text("📭 Belum ada trade.\n\n/tambah_trade untuk mencatat.")
        return
    recent = trades[-10:]
    text = f"📒 *TRADE KAMU* (10 terakhir)\n\n"
    for t in reversed(recent):
        e = "🟢" if t["type"] == "BUY" else "🔴"
        text += f"{e} *{t['type']}* {t['coin_symbol']} | {fmt_price(t['price'])} × {t['amount']} = *{fmt_price(t['total'])}*\n📅 {t['date']}\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def rekap_harian(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()
    today = date.today().isoformat()
    trades = [t for t in data["trades"].get(user_id, []) if t["date"] == today]
    if not trades:
        await update.message.reply_text(f"📭 Tidak ada trade pada {today}.")
        return
    total_buy = sum(t["total"] for t in trades if t["type"] == "BUY")
    total_sell = sum(t["total"] for t in trades if t["type"] == "SELL")
    pnl = total_sell - total_buy
    e = "🟢" if pnl >= 0 else "🔴"
    text = f"📊 *REKAP HARI INI — {today}*\n\n"
    for t in trades:
        emoji = "🟢" if t["type"] == "BUY" else "🔴"
        text += f"{emoji} {t['type']} {t['coin_symbol']} — {fmt_price(t['total'])}\n"
    text += f"\n━━━━━━━━━━━━━━\n📥 BUY: *{fmt_price(total_buy)}*\n📤 SELL: *{fmt_price(total_sell)}*\n{e} P&L: *{fmt_price(pnl)}*\n📑 Jumlah: *{len(trades)}* trade"
    await update.message.reply_text(text, parse_mode="Markdown")

async def rekap_bulanan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    data = load_data()
    month = date.today().strftime("%Y-%m")
    trades = [t for t in data["trades"].get(user_id, []) if t["date"].startswith(month)]
    if not trades:
        await update.message.reply_text("📭 Tidak ada trade bulan ini.")
        return
    total_buy = sum(t["total"] for t in trades if t["type"] == "BUY")
    total_sell = sum(t["total"] for t in trades if t["type"] == "SELL")
    pnl = total_sell - total_buy
    e = "🟢" if pnl >= 0 else "🔴"
    by_coin: Dict[str, dict] = {}
    for t in trades:
        sym = t["coin_symbol"]
        if sym not in by_coin: by_coin[sym] = {"buy": 0, "sell": 0, "count": 0}
        by_coin[sym]["buy" if t["type"]=="BUY" else "sell"] += t["total"]
        by_coin[sym]["count"] += 1
    text = f"📆 *REKAP BULAN INI — {date.today().strftime('%B %Y')}*\n\n"
    for sym, s in by_coin.items():
        pl = s["sell"] - s["buy"]
        pe = "🟢" if pl >= 0 else "🔴"
        text += f"• *{sym}*: {s['count']} trade | {pe} {fmt_price(pl)}\n"
    text += f"\n━━━━━━━━━━━━━━\n📥 BUY: *{fmt_price(total_buy)}*\n📤 SELL: *{fmt_price(total_sell)}*\n{e} P&L: *{fmt_price(pnl)}*\n📑 Total: *{len(trades)}* trade"
    await update.message.reply_text(text, parse_mode="Markdown")

async def trending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Memuat trending...")
    coins = await get_trending()
    if not coins:
        await msg.edit_text("❌ Gagal mengambil data trending.")
        return
    text = "🔥 *CRYPTO TRENDING HARI INI*\n\n"
    for i, item in enumerate(coins[:7], 1):
        c = item.get("item", {})
        text += f"{i}. *{c.get('name')}* ({c.get('symbol','').upper()}) — Rank #{c.get('market_cap_rank','?')}\n"
    text += f"\n🕐 {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    await msg.edit_text(text, parse_mode="Markdown")

async def bantuan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
📖 *PANDUAN CRYPTOBOT PRO*

📊 *HARGA & ANALISIS*
`/harga bitcoin` — Harga real-time
`/grafik bitcoin` — Chart ASCII 7/14/30 hari
`/sinyal bitcoin` — RSI, MACD, MA, Bollinger Bands
`/trending` — Crypto trending

🔔 *ALERT*
`/alert` — Pasang alert harga
`/listalert` — Lihat alert aktif
`/hapusalert` — Hapus semua alert

💼 *PORTFOLIO*
`/portfolio` — Lihat semua aset & total P&L
`/tambah_aset` — Tambah koin ke portfolio
`/hapus_aset` — Hapus koin dari portfolio

📒 *TRADE JOURNAL*
`/tambah_trade` — Catat trade BUY/SELL
`/tradesaya` — Riwayat 10 trade terakhir
`/rekap` — Rekap P&L hari ini
`/rekapbulanan` — Rekap P&L bulan ini

⚠️ _Bot ini bukan saran finansial. DYOR!_
"""
    await update.message.reply_text(text, parse_mode="Markdown")

# ============================================================
# CALLBACK HANDLER
# ============================================================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    d = query.data

    if d.startswith("grafik_"):
        parts = d.split("_")
        coin_id, sym = parts[1], parts[2]
        await _show_grafik(query.message, coin_id, sym, sym)
    elif d.startswith("chart_"):
        parts = d.split("_")
        days, coin_id, sym = int(parts[1]), parts[2], parts[3]
        await _show_grafik(query.message, coin_id, coin_id, sym, days)
    elif d.startswith("sinyal_"):
        parts = d.split("_")
        coin_id, sym = parts[1], parts[2]
        msg_obj = query.message
        await msg_obj.edit_text("⏳ Menganalisis indikator...")
        await _show_sinyal(msg_obj, coin_id, sym, sym)
    elif d.startswith("del_asset_"):
        coin_id = d.replace("del_asset_", "")
        user_id = str(update.effective_user.id)
        data = load_data()
        if user_id in data.get("portfolio", {}) and coin_id in data["portfolio"][user_id]:
            name = data["portfolio"][user_id][coin_id]["name"]
            del data["portfolio"][user_id][coin_id]
            save_data(data)
            await query.edit_message_text(f"✅ *{name}* dihapus dari portfolio.", parse_mode="Markdown")
        else:
            await query.edit_message_text("❌ Aset tidak ditemukan.")
    elif d == "refresh_portfolio":
        await portfolio_cmd(update, context)
    elif d == "menu_portfolio":
        await portfolio_cmd(update, context)
    elif d == "menu_tambah_aset":
        await query.edit_message_text("Gunakan /tambah_aset untuk menambah aset.")
    elif d == "menu_hapus_aset":
        await query.edit_message_text("Gunakan /hapus_aset untuk menghapus aset.")
    elif d == "batal":
        await query.edit_message_text("❌ Dibatalkan.")
    elif d.startswith("port_add_"):
        parts = d.split("_")
        await query.edit_message_text(
            f"Gunakan /tambah_aset untuk menambahkan koin ke portfolio.",
            parse_mode="Markdown"
        )

async def batal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Dibatalkan.\n\nKetik /start untuk menu utama.")
    return ConversationHandler.END

# ============================================================
# ALERT CHECKER
# ============================================================
async def check_alerts(app: Application):
    data = load_data()
    changed = False
    for user_id, alerts in data["alerts"].items():
        for alert in alerts:
            if alert.get("triggered"): continue
            price_data = await get_price(alert["coin_id"])
            if not price_data: continue
            current = price_data.get("usd", 0)
            target = alert["target_price"]
            triggered = (
                (alert["direction"] == "above" and current >= target) or
                (alert["direction"] == "below" and current <= target)
            )
            if triggered:
                alert["triggered"] = True
                changed = True
                dir_text = "naik di atas" if alert["direction"] == "above" else "turun di bawah"
                try:
                    await app.bot.send_message(
                        chat_id=int(user_id),
                        text=(
                            f"🔔 *ALERT TERPENUHI!*\n\n"
                            f"🪙 {alert['coin_name']} ({alert['coin_symbol']})\n"
                            f"✅ Harga sudah {dir_text} target!\n\n"
                            f"💵 Sekarang: *{fmt_price(current)}*\n"
                            f"🎯 Target: *{fmt_price(target)}*\n\n"
                            f"⚡ Saatnya action!\n\n"
                            f"Ketik `/sinyal {alert['coin_id']}` untuk analisis teknikal."
                        ),
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"Alert error {user_id}: {e}")
        data["alerts"][user_id] = [a for a in alerts if not a.get("triggered")]
    if changed:
        save_data(data)

# ============================================================
# MAIN
# ============================================================
def main():
    if BOT_TOKEN == "ISI_TOKEN_BOT_KAMU_DISINI":
        print("❌ ERROR: Set BOT_TOKEN dulu!")
        print("Cara: export BOT_TOKEN='token_kamu' atau isi langsung di kode.")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    # Alert conversation
    alert_conv = ConversationHandler(
        entry_points=[CommandHandler("alert", alert_cmd)],
        states={
            WAITING_COIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, alert_coin_received)],
            WAITING_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, alert_price_received)],
            WAITING_DIRECTION: [CallbackQueryHandler(alert_direction_received, pattern="^dir_")],
        },
        fallbacks=[CommandHandler("batal", batal)]
    )

    # Trade conversation
    trade_conv = ConversationHandler(
        entry_points=[CommandHandler("tambah_trade", tambah_trade_cmd)],
        states={
            WAITING_TRADE_COIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, trade_coin_received)],
            WAITING_TRADE_TYPE: [CallbackQueryHandler(trade_type_received, pattern="^trade_")],
            WAITING_TRADE_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, trade_price_received)],
            WAITING_TRADE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, trade_amount_received)],
            WAITING_TRADE_NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, trade_note_received)],
        },
        fallbacks=[CommandHandler("batal", batal)]
    )

    # Portfolio conversation
    portfolio_conv = ConversationHandler(
        entry_points=[
            CommandHandler("tambah_aset", tambah_aset_cmd),
            CallbackQueryHandler(lambda u, c: tambah_aset_cmd(u, c), pattern="^menu_tambah_aset$")
        ],
        states={
            WAITING_PORT_COIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, port_coin_received)],
            WAITING_PORT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, port_amount_received)],
            WAITING_PORT_BUY_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, port_buy_price_received)],
        },
        fallbacks=[CommandHandler("batal", batal)]
    )

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("bantuan", bantuan))
    app.add_handler(CommandHandler("harga", cek_harga))
    app.add_handler(CommandHandler("grafik", grafik_cmd))
    app.add_handler(CommandHandler("sinyal", sinyal_cmd))
    app.add_handler(CommandHandler("trending", trending_cmd))
    app.add_handler(CommandHandler("portfolio", portfolio_cmd))
    app.add_handler(CommandHandler("hapus_aset", hapus_aset_cmd))
    app.add_handler(CommandHandler("listalert", list_alert))
    app.add_handler(CommandHandler("hapusalert", hapus_alert))
    app.add_handler(CommandHandler("tradesaya", trade_saya))
    app.add_handler(CommandHandler("rekap", rekap_harian))
    app.add_handler(CommandHandler("rekapbulanan", rekap_bulanan))
    app.add_handler(alert_conv)
    app.add_handler(trade_conv)
    app.add_handler(portfolio_conv)
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Scheduler: cek alert tiap 1 menit
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_alerts, "interval", minutes=1, args=[app])
    scheduler.start()

    print("🤖 CryptoBot Pro berjalan! Tekan Ctrl+C untuk berhenti.")
    print("✨ Fitur: Harga | Grafik ASCII | Sinyal RSI/MACD/MA/BB | Portfolio | Alert | Trade Journal")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
