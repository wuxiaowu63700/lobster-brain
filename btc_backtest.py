"""
BTC 5分钟量化回测
策略：大周期趋势过滤 + 回撤到Fib关键位 + 反转信号入场
"""
import requests
import json
import time
import os
from datetime import datetime, timedelta

CACHE_DIR = os.path.expanduser("~/.btc_monitor/backtest_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# ── 数据获取 ──────────────────────────────────────
def get_candles(bar="5m", limit=1000, after=None, start=None):
    interval_map = {"5m": "5m", "1H": "1h", "4H": "4h", "1D": "1d"}
    binance_bar = interval_map.get(bar, bar)
    params = {"symbol": "BTCUSDT", "interval": binance_bar, "limit": str(limit)}
    if after:
        params["endTime"] = str(after)
    if start:
        params["startTime"] = str(start)
    for attempt in range(5):
        try:
            r = requests.get("https://api.binance.com/api/v3/klines",
                             params=params, timeout=20)
            data = r.json()
            if not isinstance(data, list):
                return []
            candles = []
            for c in data:
                candles.append({
                    "time": int(c[0]),
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low":  float(c[3]),
                    "close": float(c[4]),
                    "vol":  float(c[5]),
                })
            return candles
        except Exception as e:
            wait = (attempt + 1) * 3
            print(f"\n  网络错误，{wait}秒后重试({attempt+1}/5)...", end="")
            time.sleep(wait)
    print(f"\n数据获取失败，跳过此段")
    return []

def get_history(bar="5m", days=3650):
    """拉取近N天历史数据（带本地缓存）"""
    cache_file = os.path.join(CACHE_DIR, f"btc_{bar}_{days}d.json")

    # 读缓存
    if os.path.exists(cache_file):
        age = time.time() - os.path.getmtime(cache_file)
        if age < 86400:  # 1小时内的缓存直接用
            print(f"使用缓存: {bar} {days}天")
            return json.load(open(cache_file))

    print(f"拉取{days}天 {bar} 数据...")
    cutoff = int((time.time() - days * 86400) * 1000)
    now = int(time.time() * 1000)
    all_candles = []
    start = cutoff

    while start < now:
        batch = get_candles(bar=bar, limit=1000, start=start)
        if not batch:
            # 网络失败时用已有数据继续
            print(f"\n  网络中断，已收集{len(all_candles)}根，继续从断点...")
            start += 1000 * 60 * (5 if bar=="5m" else 60 if bar=="1H" else 240) * 1000
            time.sleep(5)
            continue
        all_candles.extend(batch)
        last_time = batch[-1]["time"]
        pct = (last_time - cutoff) / (now - cutoff) * 100
        print(f"  {pct:.0f}% 已拉{len(all_candles)}根...", end="\r")
        if last_time >= now:
            break
        start = last_time + 1
        time.sleep(0.5)

    # 去重排序
    seen = set()
    unique = []
    for c in sorted(all_candles, key=lambda x: x["time"]):
        if c["time"] not in seen:
            seen.add(c["time"])
            unique.append(c)
    unique = [c for c in unique if cutoff <= c["time"] <= now]
    print(f"\n获取到 {len(unique)} 根K线")

    # 保存缓存
    json.dump(unique, open(cache_file, "w"))
    return unique

# ── 技术指标 ──────────────────────────────────────
def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100
    rs = ag / al
    return round(100 - 100 / (1 + rs), 1)

def calc_ema(closes, period):
    if len(closes) < period:
        return closes[-1]
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema

def calc_macd(closes):
    if len(closes) < 26:
        return 0, 0, 0
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    macd = ema12 - ema26
    # Signal line (9 EMA of MACD) - simplified
    return macd, 0, macd

def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return 500
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs[-period:]) / period

def calc_fibonacci(candles, lookback=50):
    if len(candles) < lookback:
        return []
    recent = candles[-lookback:]
    high = max(c["high"] for c in recent)
    low  = min(c["low"]  for c in recent)
    diff = high - low
    if diff < 500:
        return []
    levels = []
    for ratio, name in [(0.236,"F23.6"),(0.382,"F38.2"),(0.5,"F50"),(0.618,"F61.8"),(0.786,"F78.6")]:
        levels.append({"price": round(high - diff * ratio, 0), "ratio": ratio, "name": name})
    return levels

def trend_direction(candles_1h, candles_4h):
    """判断大趋势方向"""
    if len(candles_4h) < 20:
        return "neutral"
    closes_4h = [c["close"] for c in candles_4h]
    rsi_4h = calc_rsi(closes_4h)
    ema20_4h = calc_ema(closes_4h, 20)
    price = closes_4h[-1]

    if rsi_4h > 55 and price > ema20_4h:
        return "up"
    elif rsi_4h < 45 and price < ema20_4h:
        return "down"
    return "neutral"

def is_trending(candles_4h, min_bb_width_pct=2.0, atr_period=14):
    """
    判断是否处于趋势行情（非震荡）
    布林带宽度 = (上轨-下轨)/中轨 × 100
    宽度 > min_bb_width_pct% 才认为是趋势行情
    """
    if len(candles_4h) < 25:
        return True  # 数据不足默认允许
    closes = [c["close"] for c in candles_4h]
    mid = sum(closes[-20:]) / 20
    std = (sum((x-mid)**2 for x in closes[-20:]) / 20) ** 0.5
    upper = mid + 2*std
    lower = mid - 2*std
    bb_width_pct = (upper - lower) / mid * 100
    return bb_width_pct >= min_bb_width_pct

# ── 信号检测 ──────────────────────────────────────
def is_near_fib(price, fib_levels, tolerance=150):
    """价格是否在Fib关键位附近"""
    for fl in fib_levels:
        if abs(price - fl["price"]) <= tolerance:
            return True, fl
    return False, None

def is_reversal_candle(candles, idx, direction):
    """检测反转K线"""
    c = candles[idx]
    body = abs(c["close"] - c["open"])
    total = c["high"] - c["low"]
    if total == 0:
        return False
    
    if direction == "long":
        # 锤子线：下影线长，收阳
        lower_wick = c["open"] - c["low"] if c["close"] > c["open"] else c["close"] - c["low"]
        return lower_wick > body * 1.5 and c["close"] > c["open"]
    else:
        # 射击之星：上影线长，收阴
        upper_wick = c["high"] - c["close"] if c["close"] < c["open"] else c["high"] - c["open"]
        return upper_wick > body * 1.5 and c["close"] < c["open"]

def vol_shrink_then_expand(candles, idx, lookback=5):
    """成交量萎缩后放量"""
    if idx < lookback + 1:
        return False
    avg_vol = sum(c["vol"] for c in candles[idx-lookback:idx]) / lookback
    prev_vol = candles[idx-1]["vol"]
    curr_vol = candles[idx]["vol"]
    return prev_vol < avg_vol * 0.7 and curr_vol > avg_vol * 1.2

# ── 回测引擎（优化版）──────────────────────────────
def build_index(candles):
    """预建时间索引，用于快速查找"""
    return [c["time"] for c in candles]

def get_subset_fast(candles, times_idx, ts, lookback):
    """用二分查找快速获取时间点前的数据"""
    import bisect
    pos = bisect.bisect_right(times_idx, ts)
    start = max(0, pos - lookback)
    return candles[start:pos]

def backtest(candles_5m, candles_1h, candles_4h, params):
    trades = []
    position = None

    # 预建索引（关键优化）
    idx_1h = build_index(candles_1h)
    idx_4h = build_index(candles_4h)

    # 预计算4H趋势和Fib（每4小时才变一次，不用每5分钟重算）
    trend_cache = {}
    fib_cache = {}
    bb_cache = {}

    print(f"  预计算趋势缓存...", end="\r")
    for i, c4 in enumerate(candles_4h):
        if i < 20:
            continue
        c4h_sub = candles_4h[max(0,i-30):i+1]
        c1h_sub = get_subset_fast(candles_1h, idx_1h, c4["time"], 50)
        if len(c1h_sub) >= 20:
            trend_cache[c4["time"]] = trend_direction(c1h_sub, c4h_sub)
        fib_cache[c4["time"]] = calc_fibonacci(c4h_sub, lookback=30)
        bb_min = params.get("min_bb_width", 0)
        if bb_min > 0:
            bb_cache[c4["time"]] = is_trending(c4h_sub, min_bb_width_pct=bb_min)
        else:
            bb_cache[c4["time"]] = True

    # 4H时间列表用于查找当前属于哪根4H
    times_4h = [c["time"] for c in candles_4h]
    import bisect

    print(f"  开始回测 {len(candles_5m)} 根K线...", end="\r")

    for i in range(100, len(candles_5m)):
        c = candles_5m[i]
        price = c["close"]
        ts = datetime.fromtimestamp(c["time"] / 1000)

        # 找当前对应的4H K线
        pos4h = bisect.bisect_right(times_4h, c["time"]) - 1
        if pos4h < 20:
            continue
        c4h_time = candles_4h[pos4h]["time"]

        # 从缓存读趋势和Fib
        trend = trend_cache.get(c4h_time, "neutral")
        fib_levels = fib_cache.get(c4h_time, [])
        bb_ok = bb_cache.get(c4h_time, True)
        
        # ── 持仓管理 ──
        if position:
            entry = position["entry"]
            direction = position["direction"]
            sl = position["sl"]
            tp = position["tp"]

            if direction == "long":
                if c["low"] <= sl:
                    trades.append({**position, "exit": sl, "pnl": sl-entry,
                                  "result": "止损", "exit_time": ts})
                    position = None; continue
                if c["high"] >= tp:
                    trades.append({**position, "exit": tp, "pnl": tp-entry,
                                  "result": "止盈", "exit_time": ts})
                    position = None; continue
            else:
                if c["high"] >= sl:
                    trades.append({**position, "exit": sl, "pnl": entry-sl,
                                  "result": "止损", "exit_time": ts})
                    position = None; continue
                if c["low"] <= tp:
                    trades.append({**position, "exit": tp, "pnl": entry-tp,
                                  "result": "止盈", "exit_time": ts})
                    position = None; continue
            continue  # 持仓中不开新仓

        # ── 开仓逻辑（用缓存）──
        if trend == "neutral" or not bb_ok or not fib_levels:
            continue

        golden_fibs = [f for f in fib_levels if params["min_fib_ratio"] <= f["ratio"] <= 0.786]
        near, fib_hit = is_near_fib(price, golden_fibs, params["fib_tolerance"])
        if not near:
            continue

        # RSI过滤（只用最近50根5m，快速计算）
        closes_5m = [x["close"] for x in candles_5m[max(0,i-50):i+1]]
        rsi = calc_rsi(closes_5m)
        
        candles_window = candles_5m[max(0,i-10):i+1]
        
        if trend == "up":
            # 做多条件：回撤到Fib支撑，RSI不超买
            if rsi > 65:
                continue
            if params["require_reversal"] and not is_reversal_candle(candles_5m, i, "long"):
                continue
            if params["require_vol"] and not vol_shrink_then_expand(candles_5m, i):
                continue
            
            entry = price
            sl = entry - params["sl_points"]
            tp = entry + params["tp_points"]
            position = {"direction": "long", "entry": entry, "sl": sl, "tp": tp,
                       "entry_time": ts, "fib": fib_hit["name"] if fib_hit else ""}
        
        elif trend == "down":
            # 做空条件：反弹到Fib阻力，RSI不超卖
            if rsi < 35:
                continue
            if params["require_reversal"] and not is_reversal_candle(candles_5m, i, "short"):
                continue
            if params["require_vol"] and not vol_shrink_then_expand(candles_5m, i):
                continue
            
            entry = price
            sl = entry + params["sl_points"]
            tp = entry - params["tp_points"]
            position = {"direction": "short", "entry": entry, "sl": sl, "tp": tp,
                       "entry_time": ts, "fib": fib_hit["name"] if fib_hit else ""}
    
    return trades

def analyze_trades_compound(trades, params, init_capital=2500, risk_pct=0.02):
    """
    动态仓位+复利回测
    每笔风险 = 本金 × risk_pct
    仓位 = 风险金额 / 止损点数
    """
    if not trades:
        print("无交易记录")
        return

    FEE_RATE = 0.0005
    FUNDING_RATE = 0.0001
    sl_pts = params["sl_points"]

    capital = init_capital
    peak_capital = init_capital
    max_dd = 0
    max_dd_pct = 0
    monthly = {}
    yearly = {}
    trade_log = []

    for t in trades:
        # 动态计算仓位（每笔最多亏本金的risk_pct）
        risk_amount = capital * risk_pct
        size = risk_amount / sl_pts  # BTC仓位
        size = round(min(size, 1.0), 3)  # 最大1BTC

        entry_price = t["entry"]
        fee = entry_price * size * FEE_RATE * 2
        funding = entry_price * size * FUNDING_RATE
        gross = t["pnl"] * size
        net = gross - fee - funding

        capital += net
        capital = max(capital, 0)

        # 回撤计算
        if capital > peak_capital:
            peak_capital = capital
        dd = peak_capital - capital
        dd_pct = dd / peak_capital * 100
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd_pct

        # 月度统计
        month = t["entry_time"].strftime("%Y-%m")
        year = t["entry_time"].strftime("%Y")
        if month not in monthly:
            monthly[month] = {"pnl": 0, "count": 0, "capital_end": capital}
        monthly[month]["pnl"] += net
        monthly[month]["count"] += 1
        monthly[month]["capital_end"] = capital

        if year not in yearly:
            yearly[year] = {"pnl": 0, "count": 0, "capital_start": capital - net}
        yearly[year]["pnl"] += net
        yearly[year]["count"] += 1

        trade_log.append({"capital": capital, "size": size, "net": net})

    final_capital = capital
    total_return = (final_capital - init_capital) / init_capital * 100
    years = len(yearly)
    cagr = ((final_capital / init_capital) ** (1/max(years,1)) - 1) * 100

    print(f"\n{'='*55}")
    print(f"【复利模拟】初始本金${init_capital:,} | 每笔风险{risk_pct*100:.0f}%")
    print(f"参数: 止损{params['sl_points']}点 止盈{params['tp_points']}点 BB>{params.get('min_bb_width',0)}%")
    print(f"{'='*55}")
    print(f"最终本金: ${final_capital:,.0f}")
    print(f"总收益率: {total_return:+.1f}%")
    print(f"年化收益(CAGR): {cagr:+.1f}%")
    print(f"最大回撤: ${max_dd:,.0f} ({max_dd_pct:.1f}%)")
    print(f"总交易: {len(trades)}笔")
    print(f"\n年度收益:")
    cap = init_capital
    for yr, v in sorted(yearly.items()):
        ret = v["pnl"] / cap * 100
        cap += v["pnl"]
        bar = "▲" if v["pnl"] >= 0 else "▼"
        print(f"  {yr}: {bar} ${v['pnl']:+,.0f} ({ret:+.1f}%) → ${cap:,.0f} ({v['count']}笔)")
    """分析回测结果（含手续费和资金费率）"""
    if not trades:
        print("无交易记录")
        return

    # ── 费用设置 ──────────────────────────────
    # OKX 合约手续费：Maker 0.02%，Taker 0.05%，用Taker保守估算
    FEE_RATE = 0.0005       # 单边0.05%，开+平 = 0.1%
    # 资金费率：每8小时收一次，平均约0.01%/次
    # 平均持仓时间：止损约1-4小时，止盈约2-8小时，估算平均4小时 = 0.5次
    FUNDING_RATE = 0.0001   # 每8小时0.01%，持仓4小时约0.005%

    wins = [t for t in trades if t["result"] == "止盈"]
    losses = [t for t in trades if t["result"] == "止损"]

    # 计算每笔手续费（按入场价格估算）
    avg_price = sum(t["entry"] for t in trades) / len(trades)
    fee_per_trade = avg_price * size * FEE_RATE * 2  # 开+平仓
    funding_per_trade = avg_price * size * FUNDING_RATE

    total_fees = (fee_per_trade + funding_per_trade) * len(trades)
    total_pnl_gross = sum(t["pnl"] * size for t in trades)
    total_pnl_net = total_pnl_gross - total_fees

    win_rate = len(wins) / len(trades) * 100
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0

    print(f"\n{'='*55}")
    print(f"参数: 止损{params['sl_points']}点 止盈{params['tp_points']}点 "
          f"Fib容差{params['fib_tolerance']}点 反转:{params['require_reversal']}")
    print(f"{'='*55}")
    print(f"总交易: {len(trades)}笔 | {len(wins)}胜 {len(losses)}负")
    print(f"胜率: {win_rate:.1f}%")
    print(f"平均盈利: +{avg_win:.0f}点 | 平均亏损: {avg_loss:.0f}点")
    print(f"盈亏比: {abs(avg_win/avg_loss):.2f}:1" if avg_loss else "无亏损")
    print(f"期望值: {(win_rate/100*avg_win + (1-win_rate/100)*avg_loss):.0f}点/笔")
    print(f"")
    print(f"── 费用分析 (0.1BTC/笔, 均价${avg_price:,.0f}) ──")
    print(f"  开平仓手续费: ${fee_per_trade:.2f}/笔 × {len(trades)}笔 = ${fee_per_trade*len(trades):,.0f}")
    print(f"  资金费率估算: ${funding_per_trade:.2f}/笔 × {len(trades)}笔 = ${funding_per_trade*len(trades):,.0f}")
    print(f"  总费用: ${total_fees:,.0f}")
    print(f"")
    print(f"── 盈亏汇总 ──")
    print(f"  毛利润: {total_pnl_gross:+.1f}U")
    print(f"  手续费: -{total_fees:.1f}U")
    print(f"  净利润: {total_pnl_net:+.1f}U ← 实际到手")

    # 最大回撤（含手续费）
    cumulative = 0
    peak = 0
    max_dd = 0
    for t in trades:
        cumulative += t["pnl"] * size - fee_per_trade - funding_per_trade
        peak = max(peak, cumulative)
        max_dd = min(max_dd, cumulative - peak)
    print(f"  最大回撤: {max_dd:.1f}U (含手续费)")

    # 月度统计（含手续费）
    print(f"\n月度净利润:")
    monthly = {}
    for t in trades:
        month = t["entry_time"].strftime("%Y-%m")
        if month not in monthly:
            monthly[month] = {"pnl": 0, "count": 0}
        monthly[month]["pnl"] += t["pnl"] * size - fee_per_trade - funding_per_trade
        monthly[month]["count"] += 1
    for m, v in sorted(monthly.items()):
        bar = "▲" if v["pnl"] >= 0 else "▼"
        print(f"  {m}: {bar} {v['pnl']:+.1f}U ({v['count']}笔)")

# ── 主程序 ──────────────────────────────────────
if __name__ == "__main__":
    print("BTC 量化回测 - 基于你的交易策略")
    print("策略：大周期趋势过滤 + Fib关键位回撤 + 反转信号入场")
    print("="*50)
    
    # 拉取数据
    candles_5m = get_history("5m", days=3650)
    time.sleep(1)
    candles_1h = get_history("1H", days=3650)
    time.sleep(1)
    candles_4h = get_history("4H", days=3650)

    if not candles_5m:
        print("数据获取失败")
        exit()

    print(f"\n数据范围: {datetime.fromtimestamp(candles_5m[0]['time']/1000).strftime('%Y-%m-%d')} "
          f"到 {datetime.fromtimestamp(candles_5m[-1]['time']/1000).strftime('%Y-%m-%d')}")

    # 测试布林带宽度过滤
    param_sets = [
        # 基准：无过滤（止盈2000最优）
        {"sl_points": 150, "tp_points": 2000, "fib_tolerance": 120,
         "require_reversal": True, "require_vol": False, "min_fib_ratio": 0.382,
         "min_bb_width": 0},
        # 轻度过滤：BB宽度>2%
        {"sl_points": 150, "tp_points": 2000, "fib_tolerance": 120,
         "require_reversal": True, "require_vol": False, "min_fib_ratio": 0.382,
         "min_bb_width": 2.0},
        # 中度过滤：BB宽度>3%
        {"sl_points": 150, "tp_points": 2000, "fib_tolerance": 120,
         "require_reversal": True, "require_vol": False, "min_fib_ratio": 0.382,
         "min_bb_width": 3.0},
        # 严格过滤：BB宽度>4%
        {"sl_points": 150, "tp_points": 2000, "fib_tolerance": 120,
         "require_reversal": True, "require_vol": False, "min_fib_ratio": 0.382,
         "min_bb_width": 4.0},
        # 止损200+止盈1500+中度过滤
        {"sl_points": 200, "tp_points": 1500, "fib_tolerance": 150,
         "require_reversal": True, "require_vol": False, "min_fib_ratio": 0.382,
         "min_bb_width": 3.0},
    ]

    names = ["止损200+止盈1500+BB>3%", "无反转K线+BB>3%", "无反转+止盈1000+BB>3%"]
    param_sets = [
        {"sl_points": 200, "tp_points": 1500, "fib_tolerance": 150,
         "require_reversal": True, "require_vol": False, "min_fib_ratio": 0.382,
         "min_bb_width": 3.0},
        {"sl_points": 200, "tp_points": 1500, "fib_tolerance": 150,
         "require_reversal": False, "require_vol": False, "min_fib_ratio": 0.382,
         "min_bb_width": 3.0},
        {"sl_points": 200, "tp_points": 1000, "fib_tolerance": 150,
         "require_reversal": False, "require_vol": False, "min_fib_ratio": 0.382,
         "min_bb_width": 3.0},
    ]

    for name, params in zip(names, param_sets):
        print(f"\n\n【{name}】")
        trades = backtest(candles_5m, candles_1h, candles_4h, params)
        for risk in [0.02, 0.05, 0.10]:
            analyze_trades_compound(trades, params, init_capital=2500, risk_pct=risk)

    print("\n\n回测完成！")
