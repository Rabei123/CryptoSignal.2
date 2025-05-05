import ccxt
import pandas as pd
import ta
from telegram.ext import Application
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import asyncio
import time
import logging
from patterns import detect_bullish_patterns
from collections import defaultdict
import io
import mplfinance as mpf
import json
import os

# === CREDENTIAL REBUILDING FROM ENV ===
credentials_str = os.getenv("GOOGLE_CREDENTIALS_JSON")
if credentials_str:
    with open("google-credentials.json", "w") as f:
        f.write(credentials_str.replace('\\n', '\n'))

# === CONFIG ===
volume_multiplier = 2
timeframes = ['4h', '1d', '1w']
take_profit_percentages = [0.05, 0.10, 0.20, 0.50]
stop_loss_percent = 0.075
SIGNAL_FILE = 'active_signals.json'
ALERTS_FILE = 'last_alerts.json'

# === TELEGRAM CONFIG ===
TELEGRAM_TOKEN = '8132154822:AAHJA0roirT1_IF3evlXvvM9JzROgk-vmAU'
TELEGRAM_CHAT_ID = '-1002435447818'

# === GOOGLE SHEET CONFIG ===
SHEET_NAME = 'CryptoSignals'
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")  # Already loaded from env

# === LOGGING ===
logging.basicConfig(filename='crypto_bot.log', level=logging.INFO, format='%(asctime)s - %(message)s')

# === INIT ===
app = Application.builder().token(TELEGRAM_TOKEN).build()
exchange = ccxt.binance()
last_alerts = {}
global_signal_timestamps = []
active_signals = {}

# === SHEET ===
def init_sheet():
    if not GOOGLE_CREDENTIALS_JSON:
        logging.error("Google credentials not found in environment variable.")
        raise ValueError("Google credentials not found in environment variable.")
    
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(GOOGLE_CREDENTIALS_JSON),
        scope=['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    )
    client = gspread.authorize(creds)
    return client.open(SHEET_NAME).sheet1

sheet = init_sheet()

# === JSON PERSISTENCE ===
def save_active_signals():
    try:
        with open(SIGNAL_FILE, 'w') as f:
            json.dump(active_signals, f, default=str)
        logging.info("Active signals saved.")
    except Exception as e:
        logging.error(f"Save error: {e}")

def load_active_signals():
    global active_signals
    if os.path.exists(SIGNAL_FILE):
        try:
            with open(SIGNAL_FILE, 'r') as f:
                data = json.load(f)
                for symbol in data:
                    if 'timestamp' in data[symbol]:
                        data[symbol]['timestamp'] = pd.to_datetime(data[symbol]['timestamp'])
                active_signals = data
                logging.info("Active signals loaded.")
        except Exception as e:
            logging.error(f"Load error: {e}")

def save_last_alerts():
    try:
        with open(ALERTS_FILE, 'w') as f:
            json.dump(last_alerts, f)
        logging.info("Last alerts saved.")
    except Exception as e:
        logging.error(f"Failed to save last alerts: {e}")

def load_last_alerts():
    global last_alerts
    if os.path.exists(ALERTS_FILE):
        try:
            with open(ALERTS_FILE, 'r') as f:
                data = json.load(f)
                last_alerts = {k: float(v) for k, v in data.items()}
                logging.info("Last alerts loaded.")
        except Exception as e:
            logging.error(f"Failed to load last alerts: {e}")

# === UTILITY FUNCTIONS ===
def log_to_sheet(symbol, tf, price, rsi, macd, macd_signal, volume, is_volume_spike, timestamp, signal_type, take_profits, stop_loss):
    try:
        sheet.append_row(
            [symbol, tf, round(price, 2), round(rsi, 2), round(macd, 4), round(macd_signal, 4), round(volume, 2), str(is_volume_spike), str(timestamp), signal_type] + take_profits + [stop_loss],
            value_input_option='USER_ENTERED'
        )
    except Exception as e:
        logging.error(f"Sheet error: {e}")

def fetch_data(symbol, timeframe):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe, limit=100)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
        macd = ta.trend.MACD(df['close'])
        df['macd'] = macd.macd()
        df['macd_signal'] = macd.macd_signal()
        df['macd_cross'] = 0
        df.loc[df['macd'] > df['macd_signal'], 'macd_cross'] = 1
        df.loc[df['macd'] < df['macd_signal'], 'macd_cross'] = -1
        df['avg_volume'] = df['volume'].rolling(window=20).mean()
        df['volume_spike'] = df['volume'] > (volume_multiplier * df['avg_volume'])
        return df
    except Exception as e:
        logging.error(f"Fetch error {symbol} {timeframe}: {e}")
        return None

def create_chart(df, symbol):
    try:
        df = df.copy().set_index('timestamp')
        fig_buf = io.BytesIO()
        mpf.plot(df[-60:], type='candle', style='charles', volume=True, title=symbol, savefig=dict(fname=fig_buf, dpi=150, bbox_inches='tight'))
        fig_buf.seek(0)
        return fig_buf
    except Exception as e:
        logging.error(f"Chart error: {e}")
        return None

async def send_telegram_message(msg, chart=None, reply_to_message_id=None):
    try:
        if chart:
            sent = await app.bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=chart, caption=msg, parse_mode='Markdown', reply_to_message_id=reply_to_message_id)
        else:
            sent = await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode='Markdown', reply_to_message_id=reply_to_message_id)
        return sent.message_id
    except Exception as e:
        logging.error(f"Telegram send error: {e}")
        return None

def limit_global_signals():
    current_time = time.time()
    global global_signal_timestamps
    global_signal_timestamps = [ts for ts in global_signal_timestamps if current_time - ts <= 86400]
    return len(global_signal_timestamps) >= 5

# === ANALYSIS ===
async def analyze(df, symbol, timeframe):
    latest = df.iloc[-1]
    if limit_global_signals():
        return
    now = time.time()
    key = f"{symbol}_{timeframe}"
    if now - last_alerts.get(key, 0) < 7200:
        return
    patterns = detect_bullish_patterns(df)
    if patterns and df['macd_cross'].iloc[-1] == 1 and df['rsi'].iloc[-1] < 70 and df['volume_spike'].iloc[-1]:
        take_profits = [round(latest['close'] * (1 + tp), 4) for tp in take_profit_percentages]
        stop_loss = round(latest['close'] * (1 - stop_loss_percent), 4)
        msg = (
            "\U0001F4C8 *Bullish Signal Detected!*\n"
            f"🔸 *Coin:* {symbol}\n"
            f"⏱ *Timeframe:* {timeframe}\n"
            f"💵 *Entry Price:* {latest['close']:.4f}\n"
            f"🎯 *TP Levels:* {', '.join([str(tp) for tp in take_profits])}\n"
            f"🛡 *Stop Loss:* {stop_loss}\n"
            f"📊 RSI: {latest['rsi']:.2f} | MACD: {latest['macd']:.2f}/{latest['macd_signal']:.2f} | Volume Spike: {latest['volume_spike']}\n"
            f"🔎 *Pattern:* {', '.join(patterns)}\n"
            f"🕒 *Time:* {latest['timestamp']}"

        )
        chart = create_chart(df.copy(), symbol)
        msg_id = await send_telegram_message(msg, chart)

        if msg_id:
            active_signals[symbol] = {
                'entry_price': latest['close'],
                'take_profits': take_profits,
                'stop_loss': stop_loss,
                'timestamp': str(latest['timestamp']),
                'hit_tps': [],
                'timeframe': timeframe,
                'telegram_msg_id': msg_id
            }
            save_active_signals()

        log_to_sheet(symbol, timeframe, latest['close'], latest['rsi'], latest['macd'], latest['macd_signal'], latest['volume'], latest['volume_spike'], latest['timestamp'], "BUY", take_profits, stop_loss)
        global_signal_timestamps.append(now)
        last_alerts[key] = now
        save_last_alerts()

# === TP / SL HANDLER ===
async def check_tp_sl_trigger(symbol, current_price, timeframe):
    if symbol in active_signals:
        signal = active_signals[symbol]
        entry_price = signal['entry_price']
        tps = signal['take_profits']
        sl = signal['stop_loss']
        already_hit = signal.get('hit_tps', [])

        tp_hit = len(already_hit) > 0
        hit_tps = [tp for tp in tps if current_price >= tp]
        new_hits = [tp for tp in hit_tps if tp not in already_hit]

        msg_update = ""

        if new_hits:
            for tp in new_hits:
                profit_percentage = ((tp - entry_price) / entry_price) * 100
                entry_time = pd.to_datetime(signal['timestamp'])
                time_diff = pd.to_datetime('now') - entry_time
                hours, remainder = divmod(int(time_diff.total_seconds()), 3600)
                minutes = remainder // 60

                msg_update += f"\n\n🎯 *Take Profit Hit!*\n" \
                              f"🔸 {symbol}\n" \
                              f"📈 *TP Target*: {tps.index(tp) + 1} ✅\n" \
                              f"Profit: {profit_percentage:.4f}% 📈\n" \
                              f"Period: {hours}h {minutes}m ⏰\n" \
                              f"📊 Current: {current_price:.4f}\n"

            signal['hit_tps'] = already_hit + new_hits
            if set(signal['hit_tps']) == set(tps):
                del active_signals[symbol]
            else:
                active_signals[symbol] = signal
            save_active_signals()

        if not tp_hit and current_price <= sl:
            msg_update += f"\n\n🛑 *Stop Loss Hit!*\n🔸 {symbol}\n📉 Entry: {entry_price} | SL: {sl}\n📊 Current: {current_price:.4f}"
            del active_signals[symbol]
            last_alerts[symbol] = time.time() - 7201
            save_active_signals()
            save_last_alerts()

            df_new = fetch_data(symbol, timeframe)
            if df_new is not None and len(df_new) > 2:
                patterns = detect_bullish_patterns(df_new)
                latest = df_new.iloc[-1]
                if patterns and df_new['macd_cross'].iloc[-1] and df_new['rsi'].iloc[-1] < 70 and df_new['volume_spike'].iloc[-1]:
                    await analyze(df_new, symbol, timeframe)

        if msg_update:
            reply_id = signal.get('telegram_msg_id')
            await send_telegram_message(msg_update, reply_to_message_id=reply_id)

# === CLEANUP ===
def cleanup_old_signals():
    current_time = time.time()
    global global_signal_timestamps
    global_signal_timestamps = [ts for ts in global_signal_timestamps if current_time - ts <= 86400]

# === MAIN LOOP ===
async def auto_run():
    while True:
        await asyncio.to_thread(exchange.load_markets)
        markets = exchange.load_markets()
        excluded_fiats = {'USD', 'EUR', 'GBP', 'TRY', 'BRL', 'AUD', 'USDC', 'USDT', 'BUSD','XUSD'}
        all_symbols = [
            symbol for symbol, data in markets.items()
            if data.get('active') and symbol.endswith('/USDT')
            and not any(x in symbol for x in [':', 'UP', 'DOWN']) and symbol.split('/')[0] not in excluded_fiats
        ]
        for symbol in all_symbols:
            for tf in timeframes:
                try:
                    df = fetch_data(symbol, tf)
                    if df is not None and len(df) > 2:
                        await analyze(df, symbol, tf)
                        await check_tp_sl_trigger(symbol, df.iloc[-1]['close'], tf)
                except Exception as e:
                    logging.error(f"Error processing {symbol} @ {tf}: {e}")
        logging.info("Cycle complete. Waiting 2 minutes...")
        cleanup_old_signals()
        await asyncio.sleep(120)

# === ENTRY POINT ===
if __name__ == "__main__":
    load_active_signals()
    load_last_alerts()
    asyncio.run(auto_run())
