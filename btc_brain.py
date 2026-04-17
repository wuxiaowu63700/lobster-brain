#!/usr/bin/env python3
"""
BTC 龙虾交易大脑 v6.9.5
- OKX K线 + Bitget 多空比/资金费率
- 动态止损（ATR）
- 急跌/急涨预警（30秒检查）
- 关键压力位共振预警（左侧入场）
- VAH/VAL/POC 价值区分析
- 自然语言识别入场/平仓指令
- 交易记录 + 自动复盘
- 白天5分钟/凌晨15分钟自适应
- 移动止损（浮盈500/800/1200点自动上移）

v6.2 新增：
- 龙虾持仓移动止损：浮盈≥500点移到保本，≥800点锁300点，≥1200点锁600点
- 止损只能往盈利方向移，不会倒退

v6.1 修复：
- MACD signal line 改用真实9周期EMA计算（原来是macd*0.85，不准确）
- load_memory 新建时补全 real_trades / total_pnl_usdt 字段
- 兼容旧memory时自动补全 total_pnl_usdt
- 清理函数内多余的 import re
- 删除 run_analysis 中重复初始化 bs 变量
"""

import requests
import json
import time
import os
import threading
import hmac
import hashlib
import base64
import re
import urllib3
from datetime import datetime

urllib3.disable_warnings()

# monkey patch requests to disable SSL verify
_orig_get = requests.get
_orig_post = requests.post
def _get(*a, **kw): kw.setdefault('verify', False); return _orig_get(*a, **kw)
def _post(*a, **kw): kw.setdefault('verify', False); return _orig_post(*a, **kw)
requests.get = _get
requests.post = _post

# ============================================================
# 配置区  ⚠️ 注意：API密钥建议移到环境变量，避免代码泄露
# 例如: export TELEGRAM_BOT_TOKEN="xxx" 然后用 os.environ.get("TELEGRAM_BOT_TOKEN")
# ============================================================
TELEGRAM_BOT_TOKEN = "8681680971:AAG7AwTQEBncL0zojd8QteijOhm-69oS__U"
TELEGRAM_CHAT_ID = "5475370058"
# 从.env文件读取密钥
def _load_env():
    env_file = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_file):
        for line in open(env_file):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()
_load_env()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")
BITGET_API_KEY = os.environ.get("BITGET_API_KEY", "")
BITGET_SECRET = "29cedf15dbd455a7542316d799fb995620d9879e38bdf906ec5c32c08c674d02"
BITGET_PASSPHRASE = "12345678123456781234567812345678"

# 交易参数
CAPITAL = 3300
POSITION_DEFAULT = 0.1
POSITION_MAX = 0.3
STOP_LOSS_MIN = 500
STOP_LOSS_MAX = 800
STOP_LOSS_DEFAULT = 650
TARGET_POINTS = 1000

# 预警阈值
CRASH_POINTS = 300
ALERT_CHECK_INTERVAL = 30
FUNDING_THRESHOLD = 0.001
OI_CHANGE_THRESHOLD = 3.0
KEY_LEVEL_PROXIMITY = 200
MIN_CONFLUENCE = 3

# 监测频率
CHECK_INTERVAL_DAY = 300
CHECK_INTERVAL_NIGHT = 900

# 文件路径
MEMORY_DIR = os.path.expanduser("~/.btc_monitor")
MEMORY_FILE = os.path.join(MEMORY_DIR, "memory.json")
LOG_FILE = os.path.join(MEMORY_DIR, "brain.log")
PRICE_HISTORY_FILE = os.path.join(MEMORY_DIR, "price_history.json")
OI_CACHE_FILE = os.path.join(MEMORY_DIR, "oi_cache.json")

os.makedirs(MEMORY_DIR, exist_ok=True)
last_alert_time = {}

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[{}] {}".format(ts, msg)
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def get_check_interval():
    hour = datetime.now().hour
    return CHECK_INTERVAL_NIGHT if 1 <= hour < 8 else CHECK_INTERVAL_DAY

def can_alert(alert_type, cooldown=300):
    now = time.time()
    if now - last_alert_time.get(alert_type, 0) > cooldown:
        last_alert_time[alert_type] = now
        return True
    return False

# ============================================================
# 记忆系统
# ============================================================

def load_memory():
    default_params = {
        "stop_loss_min": 500,
        "stop_loss_max": 800,
        "trend_target_1": 500,
        "trend_target_2": 1000,
        "trend_target_3": 2000,
        "counter_trend_target": 300,
        "position_strong": 0.2,
        "position_normal": 0.1,
        "position_counter": 0.1,
        "param_history": [],
        "last_review_count": 0
    }
    if not os.path.exists(MEMORY_FILE):
        return {
            "signals": [], "active_signal": None, "reflections": [],
            "real_trade": None, "real_trades": [],
            "bot_trade": None,
            "stats": {"total": 0, "wins": 0, "losses": 0,
                      "manual_closes": 0, "total_pnl_points": 0, "total_pnl_usdt": 0},
            "bot_stats": {"total": 0, "wins": 0, "losses": 0,
                          "total_pnl_points": 0, "total_pnl_usdt": 0, "capital": 3300},
            "dynamic_params": default_params
        }
    with open(MEMORY_FILE) as f:
        d = json.load(f)
    # 兼容旧版本
    if "bot_trade" not in d:
        d["bot_trade"] = None
    if "bot_stats" not in d:
        d["bot_stats"] = {"total": 0, "wins": 0, "losses": 0,
                          "total_pnl_points": 0, "total_pnl_usdt": 0, "capital": 3300}
    if "total_pnl_usdt" not in d.get("bot_stats", {}):
        d["bot_stats"]["total_pnl_usdt"] = d["bot_stats"].get("total_pnl_points", 0) * POSITION_DEFAULT
    if "total_pnl_usdt" not in d.get("stats", {}):
        d["stats"]["total_pnl_usdt"] = d["stats"].get("total_pnl_points", 0) * POSITION_DEFAULT
    if "real_trades" not in d:
        d["real_trades"] = []
        if d.get("real_trade"):
            d["real_trades"].append(d["real_trade"])
    # 兼容动态参数
    if "dynamic_params" not in d:
        d["dynamic_params"] = default_params
    else:
        for k, v in default_params.items():
            if k not in d["dynamic_params"]:
                d["dynamic_params"][k] = v
    return d

def save_memory(memory):
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)

def get_params(memory):
    """读取动态参数，带默认值保护"""
    p = memory.get("dynamic_params", {})
    return {
        "stop_loss_min":        p.get("stop_loss_min", 500),
        "stop_loss_max":        p.get("stop_loss_max", 800),
        "trend_target_1":       p.get("trend_target_1", 500),
        "trend_target_2":       p.get("trend_target_2", 1000),
        "trend_target_3":       p.get("trend_target_3", 2000),
        "counter_trend_target": p.get("counter_trend_target", 300),
        "position_strong":      p.get("position_strong", 0.2),
        "position_normal":      p.get("position_normal", 0.1),
        "position_counter":     p.get("position_counter", 0.1),
    }

def format_memory_for_claude(memory):
    parts = []

    # 终极目标
    goal = memory.get("goal", {})
    if goal:
        bot_stats = memory.get("bot_stats", {})
        current_capital = 3300 + bot_stats.get("total_pnl_usdt", 0)
        round_target = goal.get("round_target", 6600)
        remaining = round_target - current_capital
        parts.append("【终极目标】\n{} | 第{}轮翻倍\n本轮目标: ${:,.0f} | 当前资金: ${:,.0f} | 还差: ${:,.0f}".format(
            goal.get("ultimate", "10次翻倍"),
            goal.get("current_round", 1),
            round_target, current_capital, max(remaining, 0)))

    stats = memory["stats"]
    if stats["total"] > 0:
        winrate = stats["wins"] / stats["total"] * 100
        parts.append("【历史战绩】\n总信号: {} | 止盈: {} | 止损: {} | 手动: {}\n胜率: {:.1f}% | 累计: {:+.0f} USDT".format(
            stats["total"], stats["wins"], stats["losses"],
            stats.get("manual_closes", 0), winrate,
            stats.get("total_pnl_usdt", stats["total_pnl_points"] * 0.1)))

    if memory.get("real_trades"):
        for t in memory["real_trades"]:
            parts.append("【用户持仓】{} {} {}个 @ ${:.2f}".format(
                t.get("symbol","BTC"), t["direction"], t["size"], t["entry_price"]))
    elif memory.get("real_trade"):
        rt = memory["real_trade"]
        parts.append("【用户持仓】{} {}个 @ ${:,.0f}".format(
            rt["direction"], rt["size"], rt["entry_price"]))
    elif memory.get("active_signal"):
        sig = memory["active_signal"]
        parts.append("【龙虾追踪】{} @ ${:,.0f}".format(sig["direction"], sig["price_at_signal"]))

    recent = memory["reflections"][-2:]
    if recent:
        parts.append("【最近复盘】")
        for r in recent:
            parts.append("- {} {}".format(r["signal"], r["content"][:60]))

    bot_reflections = memory.get("bot_reflections", [])[-3:]
    if bot_reflections:
        parts.append("【龙虾历史经验】")
        for r in bot_reflections:
            parts.append("- {} {}".format(r["trade"], r.get("reflection", "")[:80]))

    # 研究助手研判（30分钟更新一次）
    rb = memory.get("research_brief")
    if rb:
        age_str = rb.get("time", "")
        parts.append("【研究助手研判({})】\n结论:{} 置信度:{} | {}\n新闻:{}\n情绪:{}".format(
            age_str,
            rb.get("conclusion", "观望"),
            rb.get("confidence", "中"),
            rb.get("core_logic", "")[:80],
            rb.get("news_summary", "")[:80],
            rb.get("social_summary", "")[:60],
        ))

    return "\n".join(parts) if parts else "暂无历史记录。"

# ============================================================
# Bitget API
# ============================================================

def bitget_sign(method, path):
    ts = str(int(time.time() * 1000))
    msg = ts + method + path
    sign = base64.b64encode(
        hmac.new(BITGET_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()
    return ts, sign

PROXIES = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}

def get_bitget_long_short():
    try:
        path = "/api/v2/mix/market/long-short?symbol=BTCUSDT&productType=USDT-FUTURES&period=1h"
        ts, sign = bitget_sign("GET", path)
        r = requests.get(
            "https://api.bitget.com" + path,
            proxies=PROXIES,
            headers={"ACCESS-KEY": BITGET_API_KEY, "ACCESS-SIGN": sign,
                     "ACCESS-TIMESTAMP": ts, "ACCESS-PASSPHRASE": BITGET_PASSPHRASE},
            timeout=8
        )
        data = r.json().get("data", [])
        if data:
            latest = data[-1]
            long_r = float(latest.get("longRatio", 0)) * 100
            short_r = float(latest.get("shortRatio", 0)) * 100
            bias = "多头主导" if long_r > 55 else "空头主导" if short_r > 55 else "均衡"
            return round(long_r, 1), round(short_r, 1), bias
    except Exception as e:
        log("Bitget多空比失败: {}".format(e))
    return None, None, None

def get_bitget_funding():
    try:
        path = "/api/v2/mix/market/current-fund-rate?symbol=BTCUSDT&productType=USDT-FUTURES"
        ts, sign = bitget_sign("GET", path)
        r = requests.get(
            "https://api.bitget.com" + path,
            proxies=PROXIES,
            headers={"ACCESS-KEY": BITGET_API_KEY, "ACCESS-SIGN": sign,
                     "ACCESS-TIMESTAMP": ts, "ACCESS-PASSPHRASE": BITGET_PASSPHRASE},
            timeout=8
        )
        data = r.json().get("data", [{}])
        if data:
            return float(data[0].get("fundingRate", 0))
    except Exception as e:
        log("Bitget资金费率失败: {}".format(e))
    return None

# ============================================================
# OKX 行情
# ============================================================

def get_current_price():
    try:
        r = requests.get("https://www.okx.com/api/v5/market/ticker",
                         params={"instId": "BTC-USDT"}, timeout=8)
        return float(r.json()["data"][0]["last"])
    except:
        return None

def get_open_interest():
    try:
        r = requests.get("https://www.okx.com/api/v5/public/open-interest",
                         params={"instId": "BTC-USDT-SWAP", "instType": "SWAP"}, timeout=8)
        return float(r.json()["data"][0]["oiCcy"])
    except:
        return None

def get_open_interest():
    try:
        r = requests.get("https://www.okx.com/api/v5/public/open-interest",
                         params={"instId": "BTC-USDT-SWAP", "instType": "SWAP"}, timeout=8)
        return float(r.json()["data"][0]["oiCcy"])
    except:
        return None

def get_liquidation_heatmap(price):
    """
    从Binance拉取最近的强平订单，统计强平密集区
    作为支撑阻力参考

    原理：强平订单聚集的价格区间 = 大量仓位的入场价附近
    多单强平密集区 = 下方支撑（多单被扫 → 价格加速下跌后可能反弹）
    空单强平密集区 = 上方压力（空单被扫 → 价格加速上涨后可能回落）

    返回: {
        "long_liq_zones": [价格区间列表，多单密集强平区，下方支撑],
        "short_liq_zones": [价格区间列表，空单密集强平区，上方压力],
        "recent_liq_total": 最近强平总额(USD),
        "liq_bias": "多头被清算" / "空头被清算" / "均衡",
        "key_levels": 基于强平推算的关键位列表
    }
    """
    try:
        # Binance 强平订单接口（免费公开）
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/allForceOrders",
            params={"symbol": "BTCUSDT", "limit": "100"},
            timeout=10
        )
        orders = r.json()
        if not isinstance(orders, list) or not orders:
            return None

        # 统计强平价格分布
        from collections import defaultdict
        long_liq  = defaultdict(float)  # 多单强平（side=SELL）
        short_liq = defaultdict(float)  # 空单强平（side=BUY）
        total_long_usd  = 0
        total_short_usd = 0

        for o in orders:
            try:
                p = float(o.get("price", 0) or o.get("averagePrice", 0))
                qty = float(o.get("origQty", 0))
                usd = p * qty
                side = o.get("side", "")
                # 强平多单 → SELL方向，强平空单 → BUY方向
                bucket = round(p / 500) * 500  # 500点一个桶
                if side == "SELL":  # 多单被强平
                    long_liq[bucket]  += usd
                    total_long_usd    += usd
                elif side == "BUY":  # 空单被强平
                    short_liq[bucket] += usd
                    total_short_usd   += usd
            except:
                continue

        total_usd = total_long_usd + total_short_usd
        if total_usd == 0:
            return None

        # 找密集强平区（超过平均值1.5倍）
        def find_dense_zones(liq_dict, threshold_ratio=1.5):
            if not liq_dict:
                return []
            avg = sum(liq_dict.values()) / len(liq_dict)
            threshold = avg * threshold_ratio
            zones = []
            for bucket, usd in sorted(liq_dict.items(), key=lambda x: x[1], reverse=True):
                if usd >= threshold and abs(bucket - price) < 5000:
                    zones.append({
                        "price": bucket,
                        "usd": round(usd / 1e6, 1),  # 转为百万USD
                        "distance": round(abs(bucket - price))
                    })
            return zones[:5]

        long_zones  = find_dense_zones(long_liq)   # 下方多单强平密集区（支撑）
        short_zones = find_dense_zones(short_liq)  # 上方空单强平密集区（压力）

        # 判断偏向
        if total_long_usd > total_short_usd * 1.5:
            liq_bias = "多头被大量清算（看空）"
        elif total_short_usd > total_long_usd * 1.5:
            liq_bias = "空头被大量清算（看多）"
        else:
            liq_bias = "多空均衡清算"

        # 生成关键位（强平密集区 = 支撑/压力）
        key_levels = []
        for z in long_zones:
            if z["price"] < price:
                key_levels.append({
                    "price": z["price"],
                    "type": "支撑",
                    "source": "多单强平密集区${:.0f}M".format(z["usd"]),
                    "usd": z["usd"]
                })
        for z in short_zones:
            if z["price"] > price:
                key_levels.append({
                    "price": z["price"],
                    "type": "压力",
                    "source": "空单强平密集区${:.0f}M".format(z["usd"]),
                    "usd": z["usd"]
                })

        log("清算热力图: 多单清算${:.0f}M 空单清算${:.0f}M | {}".format(
            total_long_usd/1e6, total_short_usd/1e6, liq_bias))

        return {
            "long_liq_zones":  long_zones,
            "short_liq_zones": short_zones,
            "total_long_usd":  round(total_long_usd / 1e6, 1),
            "total_short_usd": round(total_short_usd / 1e6, 1),
            "liq_bias":        liq_bias,
            "key_levels":      key_levels
        }

    except Exception as e:
        log("清算热力图获取失败: {}".format(e))
        return None

def get_oi_volume_analysis():
    """
    持仓量 + 成交量综合分析
    从OKX拉取最近48小时的持仓量和成交量变化
    判断资金流向

    返回: {
        "oi_trend": "增加" / "减少" / "平稳",
        "vol_trend": "放量" / "缩量" / "平稳",
        "money_flow": 资金流向描述,
        "signal": "bullish" / "bearish" / "neutral"
    }
    """
    try:
        # 拉取4H K线（含成交量）
        r = requests.get("https://www.okx.com/api/v5/market/candles",
                         params={"instId": "BTC-USDT", "bar": "4H", "limit": "12"},
                         timeout=8, verify=False)
        candles = r.json().get("data", [])
        if not candles or len(candles) < 6:
            return None

        candles.reverse()  # 时间从旧到新
        vols   = [float(c[5]) for c in candles]
        closes = [float(c[4]) for c in candles]

        # 成交量趋势（最近3根 vs 之前3根）
        recent_vol = sum(vols[-3:]) / 3
        prev_vol   = sum(vols[-6:-3]) / 3
        vol_ratio  = recent_vol / prev_vol if prev_vol > 0 else 1.0

        # 价格趋势
        price_change = (closes[-1] - closes[-6]) / closes[-6] * 100

        # 拉取OI历史
        oi_history = load_oi_history()

        oi_trend = "平稳"
        if len(oi_history) >= 6:
            recent_oi = oi_history[-1]["oi"]
            prev_oi   = oi_history[-6]["oi"]
            oi_chg    = (recent_oi - prev_oi) / prev_oi * 100 if prev_oi else 0
            if oi_chg > 1.0:   oi_trend = "明显增加"
            elif oi_chg > 0.3: oi_trend = "小幅增加"
            elif oi_chg < -1.0: oi_trend = "明显减少"
            elif oi_chg < -0.3: oi_trend = "小幅减少"
        else:
            oi_chg = 0

        vol_trend = "放量" if vol_ratio > 1.3 else "缩量" if vol_ratio < 0.7 else "平稳"

        # 综合判断资金流向
        signal = "neutral"
        if oi_trend in ["明显增加", "小幅增加"] and price_change > 0 and vol_trend == "放量":
            money_flow = "多头主动建仓+放量上涨，趋势强"
            signal = "bullish"
        elif oi_trend in ["明显增加", "小幅增加"] and price_change < 0:
            money_flow = "OI增加但价格下跌，空头建仓，看跌"
            signal = "bearish"
        elif oi_trend in ["明显减少", "小幅减少"] and price_change < 0:
            money_flow = "多头止损离场+价格下跌，下跌动能减弱"
            signal = "neutral"
        elif oi_trend in ["明显减少", "小幅减少"] and price_change > 0:
            money_flow = "空头回补推动上涨，注意可持续性"
            signal = "neutral"
        elif vol_trend == "放量" and price_change > 1:
            money_flow = "放量上涨，多头活跃"
            signal = "bullish"
        elif vol_trend == "放量" and price_change < -1:
            money_flow = "放量下跌，空头活跃"
            signal = "bearish"
        else:
            money_flow = "资金流向不明确，观望为主"

        log("资金流向: OI{} 成交量{} 价格{:+.1f}% → {}".format(
            oi_trend, vol_trend, price_change, money_flow))

        return {
            "oi_trend":    oi_trend,
            "oi_change":   round(oi_chg, 2),
            "vol_trend":   vol_trend,
            "vol_ratio":   round(vol_ratio, 2),
            "price_change": round(price_change, 2),
            "money_flow":  money_flow,
            "signal":      signal
        }

    except Exception as e:
        log("资金流向分析失败: {}".format(e))
        return None
    try:
        r = requests.get("https://www.okx.com/api/v5/market/books",
                         params={"instId": "BTC-USDT", "sz": "40"}, timeout=8)
        data = r.json()["data"][0]
        bids = [(float(b[0]), float(b[1])) for b in data["bids"]]
        asks = [(float(a[0]), float(a[1])) for a in data["asks"]]
        bid_wall = max(bids, key=lambda x: x[1]) if bids else None
        ask_wall = max(asks, key=lambda x: x[1]) if asks else None
        bid_vol = sum(b[1] for b in bids[:20])
        ask_vol = sum(a[1] for a in asks[:20])
        total = bid_vol + ask_vol
        imbalance = round((bid_vol - ask_vol) / total * 100, 1) if total > 0 else 0
        bias = "买方主导" if imbalance > 10 else "卖方主导" if imbalance < -10 else "均衡"
        return {
            "bid_wall_price": bid_wall[0] if bid_wall else None,
            "bid_wall_size": bid_wall[1] if bid_wall else None,
            "ask_wall_price": ask_wall[0] if ask_wall else None,
            "ask_wall_size": ask_wall[1] if ask_wall else None,
            "imbalance": imbalance, "bias": bias,
        }
    except:
        return {}

def get_recent_trades():
    try:
        r = requests.get("https://www.okx.com/api/v5/market/trades",
                         params={"instId": "BTC-USDT", "limit": "50"}, timeout=8)
        trades = r.json()["data"]
        lb = sum(float(t["sz"]) for t in trades if float(t["sz"]) > 5 and t["side"] == "buy")
        ls = sum(float(t["sz"]) for t in trades if float(t["sz"]) > 5 and t["side"] == "sell")
        return round(lb, 2), round(ls, 2)
    except:
        return None, None

def get_klines(bar="15m", limit=100):
    try:
        r = requests.get("https://www.okx.com/api/v5/market/candles",
                         params={"instId": "BTC-USDT", "bar": bar, "limit": limit}, timeout=10)
        candles = []
        for c in reversed(r.json()["data"]):
            candles.append({
                "time": datetime.fromtimestamp(int(c[0])/1000).strftime("%m-%d %H:%M"),
                "open": float(c[1]), "high": float(c[2]),
                "low": float(c[3]), "close": float(c[4]), "volume": float(c[5]),
            })
        return candles
    except:
        return []

def get_btc_news():
    try:
        items = []
        for q in ["Bitcoin BTC news today", "crypto market sentiment today"]:
            r = requests.get(
                "https://api.search.brave.com/res/v1/news/search",
                headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_API_KEY},
                params={"q": q, "count": 3, "freshness": "pd"}, timeout=8)
            for item in r.json().get("results", [])[:3]:
                items.append("- {} ({})".format(item.get("title", ""), item.get("age", "")))
        return "\n".join(items[:6]) if items else "暂无消息"
    except:
        return "消息获取失败"

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        d = r.json()["data"][0]
        return "{} / {}".format(d["value"], d["value_classification"])
    except:
        return "获取失败"

# ============================================================
# 技术指标
# ============================================================

def calc_atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return round(sum(trs[-period:]) / period, 1)

def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100
    return round(100 - 100/(1 + ag/al), 1)

def calc_macd(closes):
    if len(closes) < 35:
        return None, None, None
    def ema_series(data, n):
        k = 2 / (n + 1)
        e = data[0]
        result = [e]
        for p in data[1:]:
            e = p * k + e * (1 - k)
            result.append(e)
        return result
    ema12 = ema_series(closes, 12)
    ema26 = ema_series(closes, 26)
    macd_line = [a - b for a, b in zip(ema12, ema26)]
    # signal line = 9-period EMA of macd_line
    signal_line = ema_series(macd_line, 9)
    macd_val = macd_line[-1]
    sig_val = signal_line[-1]
    return round(macd_val, 1), round(sig_val, 1), round(macd_val - sig_val, 1)

def get_quant_signal(c15, c1h, c4h, price):
    """量化策略信号（10年回测最优参数：止损200/止盈2000/BB>3%/Fib回撤）"""
    try:
        if not c4h or len(c4h) < 20:
            return {"direction": None, "reason": "4H数据不足"}
        if not c1h or len(c1h) < 20:
            return {"direction": None, "reason": "1H数据不足"}

        # 4H趋势判断
        closes_4h = [c["close"] for c in c4h]
        rsi_4h = calc_rsi(closes_4h)
        ema20_4h = sum(closes_4h[-20:]) / 20

        if rsi_4h is None:
            return {"direction": None, "reason": "RSI计算失败"}

        if rsi_4h > 55 and price > ema20_4h:
            trend = "up"
        elif rsi_4h < 45 and price < ema20_4h:
            trend = "down"
        else:
            return {"direction": None, "reason": "4H趋势不明RSI{:.0f}".format(rsi_4h)}

        # 布林带宽度过滤 >3%
        closes_1h = [c["close"] for c in c1h]
        bb_u, bb_m, bb_l = calc_bollinger(closes_1h)
        if bb_u and bb_l and bb_m:
            bb_width = (bb_u - bb_l) / bb_m * 100
        else:
            return {"direction": None, "reason": "布林带计算失败"}

        if bb_width < 3.0:
            return {"direction": None, "reason": "BB宽度{:.1f}%<3%震荡市".format(bb_width)}

        # Fib关键位检测
        highs = [c["high"] for c in c4h]
        lows = [c["low"] for c in c4h]
        high = max(highs)
        low = min(lows)
        diff = high - low
        if diff < 100:
            return {"direction": None, "reason": "价格区间太小"}

        fib_levels = [
            {"ratio": 0.382, "name": "F38.2", "price": round(high - diff * 0.382, 0)},
            {"ratio": 0.5,   "name": "F50",   "price": round(high - diff * 0.5,   0)},
            {"ratio": 0.618, "name": "F61.8", "price": round(high - diff * 0.618, 0)},
            {"ratio": 0.786, "name": "F78.6", "price": round(high - diff * 0.786, 0)},
        ]

        near_fib = None
        for fib in fib_levels:
            if abs(price - fib["price"]) <= 150:
                near_fib = fib
                break

        if not near_fib:
            return {"direction": None, "reason": "未到Fib关键位"}

        if trend == "up":
            return {"direction": "做多", "sl": round(price-200,0), "tp": round(price+2000,0),
                    "reason": "量化做多:{} BB{:.1f}%".format(near_fib["name"], bb_width)}
        else:
            return {"direction": "做空", "sl": round(price+200,0), "tp": round(price-2000,0),
                    "reason": "量化做空:{} BB{:.1f}%".format(near_fib["name"], bb_width)}
    except Exception as e:
        return {"direction": None, "reason": "量化错误:{}".format(str(e)[:30])}

def calc_bollinger(closes, period=20):
    if len(closes) < period:
        return None, None, None
    rc = closes[-period:]
    mid = sum(rc)/period
    std = (sum((x-mid)**2 for x in rc)/period)**0.5
    return round(mid+2*std, 1), round(mid, 1), round(mid-2*std, 1)

def calc_volume_ratio(candles, period=20):
    if len(candles) < period+1:
        return None
    avg = sum(c["volume"] for c in candles[-period-1:-1])/period
    return round(candles[-1]["volume"]/avg, 2)

def detect_pattern(candles):
    if len(candles) < 3:
        return "数据不足"
    c, prev = candles[-1], candles[-2]
    body = abs(c["close"]-c["open"])
    upper = c["high"]-max(c["open"],c["close"])
    lower = min(c["open"],c["close"])-c["low"]
    total = c["high"]-c["low"]
    patterns = []
    if total > 0:
        if lower > body*2 and upper < body*0.5 and c["close"] > c["open"]:
            patterns.append("锤子线")
        if upper > body*2 and lower < body*0.5 and c["close"] < c["open"]:
            patterns.append("射击之星")
        if body < total*0.1:
            patterns.append("十字星")
        if (c["close"]>c["open"] and prev["close"]<prev["open"]
                and c["open"]<prev["close"] and c["close"]>prev["open"]):
            patterns.append("看涨吞没")
        if (c["close"]<c["open"] and prev["close"]>prev["open"]
                and c["open"]>prev["close"] and c["close"]<prev["open"]):
            patterns.append("看跌吞没")
    return "、".join(patterns) if patterns else "无明显形态"

def calc_vah_val_poc(candles):
    if len(candles) < 20:
        return None, None, None
    pv = {}
    for c in candles[-20:]:
        mid = round((c["high"] + c["low"]) / 2, -1)
        pv[mid] = pv.get(mid, 0) + c["volume"]
    if not pv:
        return None, None, None
    poc = max(pv, key=pv.get)
    total = sum(pv.values())
    target = total * 0.7
    sp = sorted(pv.keys())
    pi = sp.index(poc)
    cumvol = pv[poc]
    lo, hi = pi, pi
    while cumvol < target and (lo > 0 or hi < len(sp)-1):
        lv = pv[sp[lo-1]] if lo > 0 else 0
        hv = pv[sp[hi+1]] if hi < len(sp)-1 else 0
        if lv >= hv and lo > 0:
            lo -= 1; cumvol += lv
        elif hi < len(sp)-1:
            hi += 1; cumvol += hv
        else:
            break
    return sp[hi], poc, sp[lo]

def calc_fibonacci(candles, price, lookback=50):
    """计算斐波那契回撤位（基于近期高低点）"""
    if len(candles) < lookback:
        return []
    recent = candles[-lookback:]
    high = max(c["high"] for c in recent)
    low  = min(c["low"]  for c in recent)
    diff = high - low
    if diff < 500:  # 振幅太小不计算
        return []
    levels = []
    fibs = [(0.236, "Fib23.6%"), (0.382, "Fib38.2%"), (0.5, "Fib50%"),
            (0.618, "Fib61.8%"), (0.786, "Fib78.6%")]
    for ratio, name in fibs:
        fib_price = round(high - diff * ratio, 0)
        t = "压力" if fib_price > price else "支撑"
        levels.append({"price": fib_price, "type": t, "source": name,
                       "distance": round(abs(fib_price - price), 0)})
    return levels

def detect_retracement(candles_1h, candles_4h, price):
    """
    检测是否处于大涨/大跌后的回撤阶段
    返回: {
      "phase": "大涨后回撤" / "大跌后反弹" / "无明显趋势",
      "trend_dir": "up" / "down" / None,
      "move_pct": 涨跌幅,
      "retrace_pct": 已回撤比例,
      "near_key_level": 是否靠近关键位,
      "signal": "等待做空" / "等待做多" / None
    }
    """
    result = {"phase": "无明显趋势", "trend_dir": None,
              "move_pct": 0, "retrace_pct": 0,
              "near_key_level": False, "signal": None}
    try:
        # 用4H蜡烛找大趋势
        if len(candles_4h) < 10:
            return result
        # 最近10根4H K线（约40小时）
        recent_4h = candles_4h[-10:]
        high_4h = max(c["high"] for c in recent_4h)
        low_4h  = min(c["low"]  for c in recent_4h)
        # 找高点和低点的位置
        high_idx = max(range(len(recent_4h)), key=lambda i: recent_4h[i]["high"])
        low_idx  = min(range(len(recent_4h)), key=lambda i: recent_4h[i]["low"])
        move = high_4h - low_4h
        move_pct = move / low_4h * 100

        if move_pct < 2.0:  # 振幅不够，不计算
            return result

        result["move_pct"] = round(move_pct, 1)

        # 大涨后回撤：低点在前，高点在后，当前价格从高点往下回
        if low_idx < high_idx and high_idx >= len(recent_4h) - 3:
            retrace = (high_4h - price) / move * 100
            result["phase"] = "大涨后回撤"
            result["trend_dir"] = "up"
            result["retrace_pct"] = round(retrace, 1)
            # 回撤到38.2%-61.8%是黄金区间
            if 30 < retrace < 70:
                result["signal"] = "等待做多"
                result["near_key_level"] = True

        # 大跌后反弹：高点在前，低点在后，当前价格从低点往上反
        elif high_idx < low_idx and low_idx >= len(recent_4h) - 3:
            bounce = (price - low_4h) / move * 100
            result["phase"] = "大跌后反弹"
            result["trend_dir"] = "down"
            result["retrace_pct"] = round(bounce, 1)
            # 反弹到38.2%-61.8%是阻力区
            if 30 < bounce < 70:
                result["signal"] = "等待做空"
                result["near_key_level"] = True

    except Exception as e:
        pass
    return result

def calc_key_levels(candles_1h, candles_4h, price, ob):
    levels = []
    if len(candles_1h) >= 20:
        closes = [c["close"] for c in candles_1h]
        mid = sum(closes[-20:]) / 20
        std = (sum((x-mid)**2 for x in closes[-20:]) / 20) ** 0.5
        levels.append({"price": round(mid + 2*std, 0), "type": "压力", "source": "1H布林上轨"})
        levels.append({"price": round(mid - 2*std, 0), "type": "支撑", "source": "1H布林下轨"})

    vah, poc, val = calc_vah_val_poc(candles_1h)
    if vah:
        levels.append({"price": vah, "type": "压力", "source": "VAH价值区高点"})
    if val:
        levels.append({"price": val, "type": "支撑", "source": "VAL价值区低点"})
    if poc:
        levels.append({"price": poc, "type": "中枢", "source": "POC成交量峰值"})

    base = round(price / 1000) * 1000
    for offset in [-2000, -1000, 0, 1000, 2000]:
        p = base + offset
        if abs(p - price) < 2000:
            t = "压力" if p > price else "支撑"
            levels.append({"price": p, "type": t, "source": "整数关口${:,.0f}".format(p)})

    if ob and ob.get("ask_wall_price") and ob.get("ask_wall_size", 0) > 10:
        levels.append({"price": ob["ask_wall_price"], "type": "压力",
                       "source": "卖单墙({:.0f}BTC)".format(ob["ask_wall_size"])})
    if ob and ob.get("bid_wall_price") and ob.get("bid_wall_size", 0) > 10:
        levels.append({"price": ob["bid_wall_price"], "type": "支撑",
                       "source": "买单墙({:.0f}BTC)".format(ob["bid_wall_size"])})

    if len(candles_4h) >= 5:
        for h in sorted([c["high"] for c in candles_4h[-10:]], reverse=True)[:3]:
            if h > price:
                levels.append({"price": round(h, 0), "type": "压力", "source": "4H前高"})
        for l in sorted([c["low"] for c in candles_4h[-10:]])[:3]:
            if l < price:
                levels.append({"price": round(l, 0), "type": "支撑", "source": "4H前低"})

    # 加入斐波那契关键位
    fib_levels = calc_fibonacci(candles_4h, price, lookback=30)
    levels.extend(fib_levels)

    confluence = []
    processed = set()
    for i, lv in enumerate(levels):
        if i in processed:
            continue
        nearby = [lv]
        for j, lv2 in enumerate(levels):
            if i != j and j not in processed and abs(lv["price"] - lv2["price"]) <= 200:
                nearby.append(lv2)
                processed.add(j)
        processed.add(i)
        if len(nearby) >= 2:
            avg_price = round(sum(l["price"] for l in nearby) / len(nearby), 0)
            confluence.append({
                "price": avg_price, "type": nearby[0]["type"],
                "confluence": len(nearby),
                "sources": [l["source"] for l in nearby],
                "distance": round(abs(avg_price - price), 0)
            })
    return sorted(confluence, key=lambda x: x["distance"])

def calc_dynamic_sl(atr_1h):
    if not atr_1h:
        return STOP_LOSS_DEFAULT
    return max(STOP_LOSS_MIN, min(STOP_LOSS_MAX, round(atr_1h * 1.5)))

# 249天回测数据：每小时出现日高/日低的概率
HIGH_HOUR_PROB = {
    0:17.7, 1:6.0, 2:4.4, 3:2.8, 4:3.2, 5:2.0,
    6:2.4, 7:1.2, 8:2.8, 9:2.4, 10:3.2, 11:1.2,
    12:2.4, 13:0.8, 14:2.8, 15:3.2, 16:1.2, 17:2.8,
    18:1.2, 19:1.6, 20:2.8, 21:4.8, 22:9.2, 23:17.7
}
LOW_HOUR_PROB = {
    0:18.1, 1:6.0, 2:5.2, 3:4.4, 4:2.4, 5:4.8,
    6:2.8, 7:1.2, 8:2.8, 9:2.4, 10:1.2, 11:2.0,
    12:1.6, 13:2.4, 14:1.2, 15:1.6, 16:1.6, 17:1.2,
    18:0.8, 19:1.6, 20:3.2, 21:3.2, 22:10.4, 23:17.7
}

def analyze_time_session():
    """
    分析当前时段的风险特征（北京时间）
    返回: {
        "hour": 当前小时,
        "session": 时段名称,
        "high_prob": 出现日高点的历史概率,
        "low_prob": 出现日低点的历史概率,
        "risk_level": "高" / "中" / "低",
        "advice": 给龙虾的时段建议
    }
    """
    from datetime import timezone, timedelta
    now_cst = datetime.now(timezone.utc) + timedelta(hours=8)
    h = now_cst.hour

    high_prob = HIGH_HOUR_PROB.get(h, 2.0)
    low_prob  = LOW_HOUR_PROB.get(h, 2.0)
    combined  = high_prob + low_prob  # 极值出现总概率

    if h in [23, 0]:
        session = "极值高危时段"
        risk = "高"
        advice = (
            "当前{}:00是历史极值最集中时段（高点{:.1f}% 低点{:.1f}%），"
            "方向混乱波动大，开仓需放宽止损，警惕假突破，"
            "建议等待方向明确后再入场".format(h, high_prob, low_prob)
        )
    elif h in [22, 1, 2]:
        session = "高波动时段"
        risk = "高"
        advice = (
            "当前{}:00波动较大（高点{:.1f}% 低点{:.1f}%），"
            "极值可能尚未出现，止损适当放宽".format(h, high_prob, low_prob)
        )
    elif h in [21, 3, 4]:
        session = "美盘/亚盘过渡"
        risk = "中"
        advice = (
            "当前{}:00处于过渡时段（高点{:.1f}% 低点{:.1f}%），"
            "波动趋于平稳，可以正常入场".format(h, high_prob, low_prob)
        )
    elif 5 <= h <= 13:
        session = "亚洲盘"
        risk = "低"
        advice = (
            "当前{}:00亚洲盘（高点{:.1f}% 低点{:.1f}%），"
            "极值概率低，趋势延续性强，是较好的入场时机".format(h, high_prob, low_prob)
        )
    elif 14 <= h <= 20:
        session = "欧洲盘"
        risk = "低" if combined < 6 else "中"
        advice = (
            "当前{}:00欧洲盘（高点{:.1f}% 低点{:.1f}%），"
            "流动性好，可正常操作".format(h, high_prob, low_prob)
        )
    else:
        session = "美盘"
        risk = "中"
        advice = "当前{}:00美盘".format(h)

    return {
        "hour": h,
        "session": session,
        "high_prob": high_prob,
        "low_prob": low_prob,
        "combined_prob": combined,
        "risk_level": risk,
        "advice": advice
    }

def detect_level_action(candles_1h, candles_15m, price, levels):
    """
    检测关键位的突破和拒绝行为
    返回: {
        "action": "breakout_up" / "breakout_down" / "rejection_up" / "rejection_down" / None,
        "level": 触发的关键位,
        "strength": 1-3,
        "description": 描述,
        "signal": "做多" / "做空" / None,
        "sl_hint": 建议止损价,
        "tp_hint": 建议止盈价（下一个关键位）
    }
    """
    if not levels or not candles_1h or len(candles_1h) < 3:
        return {"action": None, "signal": None}

    result = {"action": None, "signal": None, "strength": 0, "description": "", "level": None}

    # 最近3根1H K线
    c0 = candles_1h[-1]  # 当前
    c1 = candles_1h[-2]  # 上一根
    c2 = candles_1h[-3]  # 上上根

    # 15分钟成交量比率
    vol_ratio = 1.0
    if candles_15m and len(candles_15m) >= 10:
        recent_vol = candles_15m[-1]["volume"]
        avg_vol = sum(c["volume"] for c in candles_15m[-10:-1]) / 9
        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1.0

    high_vol = vol_ratio > 1.3  # 成交量放大

    for lv in levels[:6]:  # 只检测最近6个关键位
        lv_price = lv["price"]
        lv_type = lv["type"]
        distance = lv["distance"]
        confluence = lv.get("confluence", 1)

        # 只检测距离在500点以内的关键位
        if distance > 500:
            continue

        # ── 突破检测 ──────────────────────────────────────
        # 向上突破压力位：前一根收盘在下方，当前价格在上方
        if lv_type in ["压力", "中枢"]:
            prev_below = c1["close"] < lv_price
            curr_above = price > lv_price
            broke_through = c0["low"] < lv_price and price > lv_price

            if prev_below and curr_above and broke_through:
                strength = 3 if (high_vol and confluence >= 3) else 2 if high_vol else 1
                result = {
                    "action": "breakout_up",
                    "level": lv,
                    "strength": strength,
                    "description": "突破{}压力位 ${:,.0f}（{}重共振）{}".format(
                        lv_type, lv_price, confluence,
                        " + 成交量放大" if high_vol else ""),
                    "signal": "做多",
                    "sl_hint": lv_price - 100,  # 止损放突破位下方
                    "tp_hint": None  # 由下一个关键位决定
                }
                log("关键位突破: 向上突破 ${:,.0f} | {}".format(lv_price, result["description"]))
                break

        # 向下突破支撑位：前一根收盘在上方，当前价格在下方
        if lv_type in ["支撑", "中枢"]:
            prev_above = c1["close"] > lv_price
            curr_below = price < lv_price
            broke_through = c0["high"] > lv_price and price < lv_price

            if prev_above and curr_below and broke_through:
                strength = 3 if (high_vol and confluence >= 3) else 2 if high_vol else 1
                result = {
                    "action": "breakout_down",
                    "level": lv,
                    "strength": strength,
                    "description": "跌破{}支撑位 ${:,.0f}（{}重共振）{}".format(
                        lv_type, lv_price, confluence,
                        " + 成交量放大" if high_vol else ""),
                    "signal": "做空",
                    "sl_hint": lv_price + 100,  # 止损放跌破位上方
                    "tp_hint": None
                }
                log("关键位突破: 向下跌破 ${:,.0f} | {}".format(lv_price, result["description"]))
                break

        # ── 拒绝检测 ──────────────────────────────────────
        # 压力位拒绝（价格触及后回落）：前一根曾经触及，但当前收盘在下方
        if lv_type in ["压力", "中枢"]:
            touched_resistance = c1["high"] >= lv_price * 0.998  # 触及或接近
            rejected = c1["close"] < lv_price and price < lv_price
            bearish_candle = c1["close"] < c1["open"]  # 阴线

            if touched_resistance and rejected and bearish_candle:
                strength = 3 if confluence >= 3 else 2 if confluence >= 2 else 1
                result = {
                    "action": "rejection_down",
                    "level": lv,
                    "strength": strength,
                    "description": "{}压力位 ${:,.0f} 拒绝，阴线回落（{}重共振）".format(
                        lv_type, lv_price, confluence),
                    "signal": "做空",
                    "sl_hint": lv_price + 80,  # 止损放拒绝位上方
                    "tp_hint": None
                }
                log("关键位拒绝: 压力拒绝 ${:,.0f} | {}".format(lv_price, result["description"]))
                break

        # 支撑位拒绝（价格触及后反弹）：前一根曾经触及，但当前收盘在上方
        if lv_type in ["支撑", "中枢"]:
            touched_support = c1["low"] <= lv_price * 1.002
            rejected = c1["close"] > lv_price and price > lv_price
            bullish_candle = c1["close"] > c1["open"]  # 阳线

            if touched_support and rejected and bullish_candle:
                strength = 3 if confluence >= 3 else 2 if confluence >= 2 else 1
                result = {
                    "action": "rejection_up",
                    "level": lv,
                    "strength": strength,
                    "description": "{}支撑位 ${:,.0f} 拒绝，阳线反弹（{}重共振）".format(
                        lv_type, lv_price, confluence),
                    "signal": "做多",
                    "sl_hint": lv_price - 80,  # 止损放支撑位下方
                    "tp_hint": None
                }
                log("关键位拒绝: 支撑反弹 ${:,.0f} | {}".format(lv_price, result["description"]))
                break

    # 找止盈目标（下一个关键位）
    if result.get("signal") == "做多":
        targets_above = sorted([l for l in levels if l["price"] > price], key=lambda x: x["price"])
        if targets_above:
            result["tp_hint"] = targets_above[0]["price"]
    elif result.get("signal") == "做空":
        targets_below = sorted([l for l in levels if l["price"] < price], key=lambda x: x["price"], reverse=True)
        if targets_below:
            result["tp_hint"] = targets_below[0]["price"]

    return result

def detect_market_state(market_data):
    """
    识别市场状态：单边趋势 / 震荡区间 / 高波动 / 转折点
    返回: (state, description, should_trade, score_long, score_short, trend_bias)
    trend_bias: "多" / "空" / None
    """
    d1  = market_data.get("日线", {})
    h4  = market_data.get("4小时", {})
    h1  = market_data.get("1小时", {})

    rsi_1d  = d1.get("RSI") or 50
    rsi_4h  = h4.get("RSI") or 50
    rsi_1h  = h1.get("RSI") or 50
    macd_1h = h1.get("Histogram") or 0
    macd_4h = h4.get("Histogram") or 0
    vol_1h  = h1.get("成交量比率") or 1.0
    pos_1h  = h1.get("价格位置", "中间")
    pos_4h  = h4.get("价格位置", "中间")

    # OI背离信号
    oi_div = market_data.get("OI背离", {})
    oi_signal   = oi_div.get("信号", "neutral")
    oi_strength = oi_div.get("强度", 0)

    score_long  = 0
    score_short = 0
    score_vol   = 0

    if rsi_1d > 58: score_long  += 3
    elif rsi_1d > 52: score_long += 1
    if rsi_1d < 42: score_short += 3
    elif rsi_1d < 48: score_short += 1
    if rsi_4h > 55: score_long  += 2
    if rsi_4h < 45: score_short += 2
    if rsi_1h > 60: score_long  += 1
    if rsi_1h < 40: score_short += 1

    if rsi_1h > 75 or rsi_1h < 25: score_vol += 1
    if rsi_4h > 70 or rsi_4h < 30: score_vol += 1

    if macd_1h > 0: score_long  += 1
    if macd_1h < 0: score_short += 1
    if macd_4h > 0: score_long  += 2
    if macd_4h < 0: score_short += 2

    if vol_1h and vol_1h > 1.5: score_vol += 1

    if pos_1h == "超买": score_short += 1
    if pos_1h == "超卖": score_long  += 1
    if pos_4h == "超买": score_short += 2
    if pos_4h == "超卖": score_long  += 2

    # OI背离加分（强度越高权重越大）
    if oi_signal == "bearish":
        score_short += oi_strength
        log("OI背离看跌，空头加{}分".format(oi_strength))
    elif oi_signal == "bullish":
        score_long += oi_strength
        log("OI背离看涨，多头加{}分".format(oi_strength))

    # 单边趋势检测
    consecutive_up = 0
    consecutive_down = 0
    trend_bias = None
    try:
        h4_price   = h4.get("价格", 0) or 0
        h4_bb_up   = h4.get("布林上", 0) or 0
        h4_bb_low  = h4.get("布林下", 0) or 0
        if rsi_1d < 40 and rsi_4h < 40 and macd_4h < 0:
            consecutive_down = 3
        elif rsi_1d > 60 and rsi_4h > 60 and macd_4h > 0:
            consecutive_up = 3
        if h4_bb_low and h4_price and h4_price < h4_bb_low and rsi_1d < 45:
            consecutive_down = max(consecutive_down, 2)
        if h4_bb_up and h4_price and h4_price > h4_bb_up and rsi_1d > 55:
            consecutive_up = max(consecutive_up, 2)
    except:
        pass

    is_one_sided_down = consecutive_down >= 2
    is_one_sided_up   = consecutive_up >= 2

    if is_one_sided_down:
        trend_bias = "空"
        score_short += 3
    elif is_one_sided_up:
        trend_bias = "多"
        score_long += 3

    trend_strength = abs(score_long - score_short)
    dominant = "多头" if score_long > score_short else "空头"

    if is_one_sided_down:
        state = "单边下跌"
        desc = "多周期空头共振，逆势做多是陷阱，只做顺势或观望"
        should_trade = True
    elif is_one_sided_up:
        state = "单边上涨"
        desc = "多周期多头共振，逆势做空是陷阱，只做顺势或观望"
        should_trade = True
    elif score_vol >= 3 and trend_strength < 2:
        state = "高波动震荡"
        desc = "多空交织，波动大，不适合持仓"
        should_trade = False
        trend_bias = None
    elif trend_strength >= 4:
        state = "强势{}趋势".format(dominant)
        desc = "多周期共振，{}方向明确".format(dominant)
        should_trade = True
        trend_bias = "多" if dominant == "多头" else "空"
    elif trend_strength >= 2:
        state = "{}趋势".format(dominant)
        desc = "趋势偏{}，但不够强".format(dominant)
        should_trade = True
    elif score_vol >= 2:
        state = "震荡偏{}".format(dominant)
        desc = "市场震荡，轻仓短线为主"
        should_trade = True
    else:
        state = "震荡观望"
        desc = "方向不明，等待更好机会"
        should_trade = False

    return state, desc, should_trade, score_long, score_short, trend_bias

def collect_data():
    log("拉取行情数据...")
    price = get_current_price()
    oi = get_open_interest()
    ob = None  # get_orderbook不在此版本
    lb, ls = get_recent_trades()
    funding = get_bitget_funding()
    long_r, short_r, ls_bias = get_bitget_long_short()

    # OI历史记录 + 背离分析
    if oi and price:
        save_oi_history(oi, price)
    oi_signal, oi_desc, oi_strength = analyze_oi_divergence(oi, price)

    # 清算热力图
    liq_map = get_liquidation_heatmap(price) if price else None

    # 持仓量+成交量资金流向分析
    oi_vol = get_oi_volume_analysis()

    c15 = get_klines("15m", 100)
    c1h = get_klines("1H", 100)
    c4h = get_klines("4H", 100)
    c1d = get_klines("1D", 100)

    key_levels = calc_key_levels(c1h, c4h, price, ob) if c1h and c4h and price and ob else []

    # 把清算热力图的关键位加入key_levels
    if liq_map and liq_map.get("key_levels"):
        key_levels = key_levels + liq_map["key_levels"]
        key_levels = sorted(key_levels, key=lambda x: x.get("distance", abs(x["price"] - price) if price else 9999))
    atr_1h = calc_atr(c1h) if c1h else None
    sl = calc_dynamic_sl(atr_1h)

    # 关键位突破/拒绝检测
    level_action = detect_level_action(c1h, c15, price, key_levels) if c1h and price else {"action": None, "signal": None}

    # 时段风险分析
    time_session = analyze_time_session()
    log("时段分析: {} | {} | 风险:{}".format(
        time_session["session"], time_session["advice"][:30], time_session["risk_level"]))

    result = {
        "当前价格": price,
        "动态止损点数": sl,
        "资金费率": "{:.4%}".format(funding) if funding is not None else "获取失败",
        "持仓量(BTC)": round(oi, 0) if oi else "获取失败",
        "订单簿": ob,
        "近期大单买入": lb, "近期大单卖出": ls,
        "恐惧贪婪指数": get_fear_greed(),
        "最新消息面": get_btc_news(),
        "关键压力支撑位": key_levels[:6],
        "Bitget多空比": {
            "多头": "{}%".format(long_r) if long_r else "获取失败",
            "空头": "{}%".format(short_r) if short_r else "获取失败",
            "偏向": ls_bias or "获取失败",
        },
        "OI背离": {
            "信号": oi_signal,
            "描述": oi_desc,
            "强度": oi_strength,
        },
        "关键位行为": level_action,
        "时段分析": time_session,
        "清算热力图": liq_map,
        "资金流向": oi_vol,
        "回撤分析": detect_retracement(c1h, c4h, price),
    }

    for name, candles in [("日线",c1d),("4小时",c4h),("1小时",c1h),("15分钟",c15)]:
        if not candles:
            continue
        closes = [c["close"] for c in candles]
        p = closes[-1]
        rsi = calc_rsi(closes)
        macd, sig, hist = calc_macd(closes)
        bb_u, bb_m, bb_l = calc_bollinger(closes)
        vol = calc_volume_ratio(candles)
        atr = calc_atr(candles)
        position = "超买" if bb_u and p > bb_u else "超卖" if bb_l and p < bb_l else "中间"
        result[name] = {
            "价格": p, "RSI": rsi,
            "MACD": macd, "Signal": sig, "Histogram": hist,
            "布林上": bb_u, "布林中": bb_m, "布林下": bb_l,
            "价格位置": position, "成交量比率": vol,
            "ATR": atr, "K线形态": detect_pattern(candles),
        }
        time.sleep(0.2)

    # 加入量化信号
    try:
        c15 = get_klines("15m", 100)
        c1h_q = get_klines("1H", 50)
        c4h_q = get_klines("4H", 30)
        result["量化信号"] = get_quant_signal(c15, c1h_q, c4h_q, price)
    except Exception as e:
        result["量化信号"] = {"direction": None, "reason": "量化错误:{}".format(str(e)[:30])}

    return result

# ============================================================
# 价格历史（急跌检测）
# ============================================================

def load_price_history():
    if not os.path.exists(PRICE_HISTORY_FILE):
        return []
    with open(PRICE_HISTORY_FILE) as f:
        return json.load(f)

def save_price_history(history):
    now = time.time()
    history = [h for h in history if now - h["ts"] <= 300]
    with open(PRICE_HISTORY_FILE, "w") as f:
        json.dump(history, f)

def load_oi_cache():
    if not os.path.exists(OI_CACHE_FILE):
        return None
    with open(OI_CACHE_FILE) as f:
        return json.load(f).get("oi")

def save_oi_cache(oi):
    with open(OI_CACHE_FILE, "w") as f:
        json.dump({"oi": oi, "ts": time.time()}, f)

def load_oi_history():
    """读取OI历史（用于背离分析）"""
    oi_history_file = os.path.join(MEMORY_DIR, "oi_history.json")
    if not os.path.exists(oi_history_file):
        return []
    try:
        with open(oi_history_file) as f:
            return json.load(f)
    except:
        return []

def save_oi_history(oi, price):
    """保存OI历史，保留最近48条（约4小时，每5分钟一条）"""
    oi_history_file = os.path.join(MEMORY_DIR, "oi_history.json")
    history = load_oi_history()
    history.append({"oi": oi, "price": price, "ts": time.time()})
    history = history[-48:]
    with open(oi_history_file, "w") as f:
        json.dump(history, f)

def analyze_oi_divergence(oi, price):
    """
    OI背离分析：
    - OI上涨 + 价格下跌/横盘 → 空头建仓，看跌信号
    - OI上涨 + 价格上涨 → 多头建仓，看涨信号
    - OI下跌 + 价格下跌 → 空头平仓/多头止损，下跌动能减弱
    - OI下跌 + 价格上涨 → 多头平仓/空头止损，上涨动能减弱
    返回: (signal, description, strength)
    signal: "bearish" / "bullish" / "neutral"
    strength: 1-3
    """
    if not oi or not price:
        return "neutral", "OI数据不足", 0

    history = load_oi_history()
    if len(history) < 3:
        return "neutral", "OI历史不足", 0

    # 取最近6条（约30分钟）和12条（约1小时）做对比
    recent_6  = history[-6:]  if len(history) >= 6  else history
    recent_12 = history[-12:] if len(history) >= 12 else history

    # 计算OI变化
    oi_change_6  = (oi - recent_6[0]["oi"])  / recent_6[0]["oi"]  * 100 if recent_6[0]["oi"]  else 0
    oi_change_12 = (oi - recent_12[0]["oi"]) / recent_12[0]["oi"] * 100 if recent_12[0]["oi"] else 0

    # 计算价格变化
    price_change_6  = (price - recent_6[0]["price"])  / recent_6[0]["price"]  * 100 if recent_6[0]["price"]  else 0
    price_change_12 = (price - recent_12[0]["price"]) / recent_12[0]["price"] * 100 if recent_12[0]["price"] else 0

    signal = "neutral"
    desc = ""
    strength = 0

    # 核心背离判断
    oi_rising_6  = oi_change_6  > 0.3   # OI上涨超过0.3%
    oi_rising_12 = oi_change_12 > 0.5
    oi_falling_6  = oi_change_6  < -0.3
    oi_falling_12 = oi_change_12 < -0.5

    price_rising_6   = price_change_6  > 0.1
    price_falling_6  = price_change_6  < -0.1
    price_flat_6     = abs(price_change_6) <= 0.1

    # 1. OI上涨 + 价格下跌 → 最强看跌信号（空头在建仓）
    if oi_rising_6 and price_falling_6:
        signal = "bearish"
        strength = 3 if oi_rising_12 else 2
        desc = "OI{:+.1f}% 价格{:+.1f}% → 空头建仓，看跌".format(oi_change_6, price_change_6)

    # 2. OI上涨 + 价格横盘 → 空头蓄力，看跌
    elif oi_rising_6 and price_flat_6:
        signal = "bearish"
        strength = 2 if oi_rising_12 else 1
        desc = "OI{:+.1f}% 价格横盘 → 空头蓄力，潜在下跌".format(oi_change_6)

    # 3. OI上涨 + 价格上涨 → 多头建仓，看涨
    elif oi_rising_6 and price_rising_6:
        signal = "bullish"
        strength = 3 if oi_rising_12 else 2
        desc = "OI{:+.1f}% 价格{:+.1f}% → 多头建仓，看涨".format(oi_change_6, price_change_6)

    # 4. OI下跌 + 价格下跌 → 多头止损，下跌动能减弱（可能反弹）
    elif oi_falling_6 and price_falling_6:
        signal = "neutral"
        strength = 1
        desc = "OI{:+.1f}% 价格{:+.1f}% → 多头出逃，下跌动能减弱".format(oi_change_6, price_change_6)

    # 5. OI下跌 + 价格上涨 → 空头止损，上涨动能减弱
    elif oi_falling_6 and price_rising_6:
        signal = "neutral"
        strength = 1
        desc = "OI{:+.1f}% 价格{:+.1f}% → 空头回补，上涨动能减弱".format(oi_change_6, price_change_6)

    else:
        desc = "OI{:+.1f}% 价格{:+.1f}% → 无明显背离".format(oi_change_6, price_change_6)

    log("OI背离: {} | {}".format(signal, desc))
    return signal, desc, strength

# ============================================================
# 预警线程
# ============================================================

def alert_thread():
    log("急跌预警线程启动")
    while True:
        try:
            price = get_current_price()
            if not price:
                time.sleep(ALERT_CHECK_INTERVAL)
                continue

            now_ts = time.time()
            history = load_price_history()
            history.append({"price": price, "ts": now_ts})
            save_price_history(history)

            # 急跌检测
            one_min = [h for h in history if now_ts - h["ts"] <= 60]
            if one_min:
                change = price - one_min[0]["price"]
                if abs(change) >= CRASH_POINTS and can_alert("crash", 180):
                    direction = "急跌" if change < 0 else "急涨"
                    msg = "<b>BTC {} 预警</b>\n".format(direction)
                    msg += "1分钟变动: {:+.0f}点\n".format(change)
                    msg += "当前价: ${:,.0f}\n".format(price)
                    msg += "注意风险，留意关键位反应！"
                    log("TG已关闭: " + str(msg))
                    log("急跌/急涨预警已推送")

            # 关键位预警
            c1h = get_klines("1H", 50)
            c4h = get_klines("4H", 20)
            c15 = get_klines("15m", 20)
            ob = None  # get_orderbook不在此版本
            if c1h and c4h:
                levels = calc_key_levels(c1h, c4h, price, ob)
                memory_check = load_memory()
                has_position = bool(memory_check.get("real_trades")) or bool(memory_check.get("real_trade"))

                # ── 关键位突破/拒绝实时检测 ──────────────────
                la = detect_level_action(c1h, c15, price, levels)
                if la.get("action") and la.get("strength", 0) >= 2:
                    action_key = "level_action_{}_{}".format(la["action"], int(la["level"]["price"]) if la.get("level") else 0)
                    if can_alert(action_key, cooldown=1800):
                        action_emoji = {
                            "breakout_up": "🚀", "breakout_down": "📉",
                            "rejection_up": "🟢", "rejection_down": "🔴"
                        }.get(la["action"], "⚡")
                        action_name = {
                            "breakout_up": "向上突破", "breakout_down": "向下跌破",
                            "rejection_up": "支撑反弹", "rejection_down": "压力拒绝"
                        }.get(la["action"], "关键位行为")
                        msg = "{} <b>关键位{}</b>\n".format(action_emoji, action_name)
                        msg += "─────────────────────\n"
                        msg += "{}\n".format(la.get("description", ""))
                        msg += "当前价: ${:,.0f} | 强度: {}/3\n".format(price, la["strength"])
                        if la.get("signal"):
                            msg += "建议方向: <b>{}</b>\n".format(la["signal"])
                        if la.get("sl_hint"):
                            msg += "建议止损: ${:,.0f}\n".format(la["sl_hint"])
                        if la.get("tp_hint"):
                            msg += "目标位: ${:,.0f}\n".format(la["tp_hint"])
                        msg += "─────────────────────"
                        log("TG已关闭: " + str(msg))
                        log("关键位行为预警(已关闭): {} ${:,.0f}".format(la["action"], price))

                for lv in levels:
                    if (lv["distance"] <= KEY_LEVEL_PROXIMITY and
                            lv["confluence"] >= MIN_CONFLUENCE and
                            not has_position and
                            can_alert("key_{}".format(int(lv["price"])), 1800)):

                        # 让Claude快速判断这个位置值不值得入场
                        rsi_1h = None
                        macd_1h = None
                        try:
                            closes = [c["close"] for c in c1h]
                            rsi_1h = calc_rsi(closes)
                            macd_1h, _, _ = calc_macd(closes)
                        except:
                            pass

                        quick_prompt = (
                            "BTC现在${:,.0f}，正在接近{}位${:,.0f}（{}重共振，{}）。\n"
                            "RSI(1H)={} MACD(1H)={} 订单簿={}\n"
                            "用一句话判断：这里值得{}吗？值得就说'值得'，不值得就说'不值得'，给一个理由。"
                        ).format(
                            price, lv["type"], lv["price"], lv["confluence"],
                            "、".join(lv["sources"][:2]),
                            rsi_1h, macd_1h,
                            ob.get("bias", "N/A") if ob else "N/A",
                            "做空" if lv["type"] == "压力" else "做多"
                        )

                        try:
                            quick_result = claude_request(quick_prompt, max_tokens=80)
                            if quick_result and "值得" in quick_result and "不值得" not in quick_result:
                                d = "做空" if lv["type"] == "压力" else "做多"
                                zone_low = int(lv["price"]) - 100
                                zone_high = int(lv["price"]) + 100
                                src = "、".join(lv["sources"][:3])
                                msg = "<b>关键位机会预警</b>\n"
                                msg += "─────────────────────\n"
                                msg += "位置: ${:,.0f}（{} {}重共振）\n".format(lv["price"], lv["type"], lv["confluence"])
                                msg += "来源: {}\n\n".format(src)
                                msg += "建议区间: ${:,} - ${:,}\n".format(zone_low, zone_high)
                                msg += "方向: <b>{}</b>\n".format(d)
                                msg += "龙虾判断: {}\n".format(quick_result.strip())
                                msg += "─────────────────────\n"
                                msg += "<i>30分钟内不再重复</i>"
                                log("TG已关闭: " + str(msg))
                                log("关键位预警已跳过(已关闭): ${}".format(lv["price"]))
                            else:
                                log("关键位${} 龙虾判断不值得，跳过推送".format(int(lv["price"])))
                        except Exception as e:
                            log("关键位Claude判断失败: {}".format(e))

            # 资金费率预警
            funding = get_bitget_funding()
            if funding is not None and abs(funding) > FUNDING_THRESHOLD:
                if can_alert("funding", 1800):
                    bias = "多头过热" if funding > 0 else "空头过热"
                    msg = "<b>资金费率异常</b>\n费率: {:.4%}\n状态: {}\n当前价: ${:,.0f}".format(
                        funding, bias, price)
                    log("TG已关闭: " + str(msg))

            # OI突变 + 背离预警
            oi = get_open_interest()
            prev_oi = load_oi_cache()
            if oi and price:
                save_oi_history(oi, price)
                # 背离分析
                oi_signal, oi_desc, oi_strength = analyze_oi_divergence(oi, price)
                # 强背离预警（强度>=2才推送）
                if oi_strength >= 2 and can_alert("oi_divergence_{}".format(oi_signal), 1800):
                    emoji = "🔴" if oi_signal == "bearish" else "🟢"
                    title = "OI背离看跌预警" if oi_signal == "bearish" else "OI背离看涨预警"
                    msg = "{} <b>{}</b>\n".format(emoji, title)
                    msg += "{}\n".format(oi_desc)
                    msg += "当前价: ${:,.0f} | 强度: {}/3\n".format(price, oi_strength)
                    if funding is not None:
                        # OI看跌 + 资金费率为正 → 三重确认，极强空头信号
                        if oi_signal == "bearish" and funding > 0:
                            msg += "⚠️ <b>三重确认</b>: OI背离+资金费率正值，空头信号极强！\n"
                        elif oi_signal == "bullish" and funding < 0:
                            msg += "⚠️ <b>三重确认</b>: OI背离+资金费率负值，多头信号极强！\n"
                    log("TG已关闭: " + str(msg))

            if oi and prev_oi:
                oi_change = (oi - prev_oi) / prev_oi * 100
                if abs(oi_change) > OI_CHANGE_THRESHOLD and can_alert("oi", 600):
                    d = "增加" if oi_change > 0 else "减少"
                    msg = "<b>持仓量突变</b>\n持仓{}: {:+.1f}%\n当前价: ${:,.0f}".format(
                        d, oi_change, price)
                    log("TG已关闭: " + str(msg))
            if oi:
                save_oi_cache(oi)

        except Exception as e:
            log("预警线程异常: {}".format(e))
        time.sleep(ALERT_CHECK_INTERVAL)

# ============================================================
# Claude 分析
# ============================================================

def claude_request(prompt, max_tokens=1500):
    try:
        r = requests.post(
            "https://api.gptsapi.net/v1/chat/completions",
            headers={"Authorization": "Bearer {}".format(OPENROUTER_API_KEY),
                     "Content-Type": "application/json"},
            json={"model": "gpt-4o-mini",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": max_tokens},
            timeout=45
        )
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log("Claude请求失败: {}".format(e))
        return None

def analyze_main(market_data, memory_context):
    price = market_data.get("当前价格", 0)
    sl = market_data.get("动态止损点数", STOP_LOSS_DEFAULT)
    rr = round(TARGET_POINTS/sl, 1)
    levels = market_data.get("关键压力支撑位", [])
    levels_str = ""
    for lv in levels[:4]:
        src = "+".join(lv.get("sources", [lv.get("source","")])) if lv.get("sources") else lv.get("source","")
        levels_str += "{}{}${:,.0f}({}重共振) ".format(
            lv["type"], "距{}点".format(int(lv["distance"])), lv["price"], lv.get("confluence",1))

    # 回撤分析
    ret = market_data.get("回撤分析", {})
    ret_str = "无明显趋势"
    if ret.get("phase") != "无明显趋势":
        ret_str = "{}：近期涨跌幅{:.1f}% 已回撤{:.1f}% {}".format(
            ret.get("phase",""), ret.get("move_pct",0),
            ret.get("retrace_pct",0),
            "→ 建议{}".format(ret.get("signal")) if ret.get("signal") else ""
        )

    # 清算热力图
    liq = market_data.get("清算热力图") or {}
    liq_str = "无数据"
    if liq:
        liq_str = "{}（多头清算${:.0f}M 空头清算${:.0f}M）".format(
            liq.get("liq_bias",""), liq.get("total_long_usd",0), liq.get("total_short_usd",0))
        if liq.get("key_levels"):
            for kl in liq["key_levels"][:2]:
                liq_str += " {}{}${:,.0f}".format(kl["type"],kl["source"][:8],kl["price"])

    # 资金流向
    ov = market_data.get("资金流向") or {}
    ov_str = "无数据"
    if ov:
        ov_str = "{} | OI{} 成交量{} 价格{:+.1f}%".format(
            ov.get("money_flow",""), ov.get("oi_trend",""), ov.get("vol_trend",""), ov.get("price_change",0))

    ts = market_data.get("时段分析", {})
    ts_str = "{}（风险:{} 高点概率{:.1f}% 低点概率{:.1f}%）— {}".format(
        ts.get("session","未知"), ts.get("risk_level","中"),
        ts.get("high_prob",0), ts.get("low_prob",0), ts.get("advice","")[:40]
    ) if ts else "未知"

    la = market_data.get("关键位行为", {})
    la_desc = la.get("description","无") if la.get("action") else "无明显突破/拒绝"
    la_signal = la.get("signal","")
    la_strength = la.get("strength",0)
    la_sl = la.get("sl_hint")
    la_tp = la.get("tp_hint")
    level_action_str = "{}{}{}".format(
        la_desc,
        " → 建议{}".format(la_signal) if la_signal else "",
        " 止损${:,.0f} 目标${:,.0f}".format(la_sl,la_tp) if la_sl and la_tp else ""
    )

    prompt = (
        "你是BTC合约交易员，使用以下核心策略（这是经过验证的盈利方法）：\n"
        "【核心策略】大涨或大跌（小时级别）后，等价格回撤到趋势线/斐波那契黄金位/支撑阻力，"
        "然后观察是否反弹无力或回调无力，确认后才入场。\n"
        "具体：大跌后反弹→反弹到阻力位无力突破→做空；大涨后回调→回调到支撑位无力跌破→做多。\n"
        "【禁止行为】1.在趋势中追末端 2.RSI超买/超卖就开反向 3.连续止损后加仓 4.逆大趋势\n"
        "【仓位原则】首仓小（0.1BTC），方向确认+盈利后顺势加仓，亏损不加仓\n\n"
        "数据: 价格${} | 止损{}点 | 目标{}点\n"
        "记忆: {}\n"
        "关键位(含Fib): {}\n"
        "回撤状态(重要): {}\n"
        "日线RSI={} 4H RSI={} 1H RSI={} | 资金费率={} 多空比={}\n"
        "OI背离: {} (强度:{}/3)\n"
        "关键位行为: {} (强度:{}/3)\n"
        "清算热力图: {}\n"
        "资金流向: {}\n"
        "时段: {}\n"
        "消息: {}\n\n"
        "判断步骤：\n"
        "1. 当前是大涨后回调还是大跌后反弹？（看回撤状态）\n"
        "2. 价格是否在斐波那契38.2%-61.8%区间内？\n"
        "3. 是否有关键位共振（2个以上指标重合）？\n"
        "4. 反弹/回调是否已经无力（成交量萎缩、RSI背离）？\n"
        "5. 大趋势（日线/4H）方向是否支持这笔交易？\n\n"
        "必须严格按以下格式输出8行，每行有【】标签：\n"
        "行1: 当前行情一句话（重点说明回撤状态）\n"
        "行2: 判断做多/做空/观望，原因（必须说关键位在哪）\n"
        "行3: 入场区间 止损 目标\n"
        "行4: 最大风险（如果是观望，说明等什么条件才入场）\n"
        "行5: 必须写【判断】做多 或 【判断】做空 或 【判断】观望\n"
        "行6: 必须写【强度】强 或 【强度】中 或 【强度】弱\n"
        "行7: 必须写【推送】是 或 【推送】否\n"
        "行8: 必须写【理由】然后一句话\n"
        "不能有任何标题、表格、多余文字"
    ).format(
        int(price), sl, TARGET_POINTS,
        memory_context[:150],
        levels_str if levels_str else "无",
        ret_str,
        market_data.get("日线",{}).get("RSI","N/A"),
        market_data.get("4小时",{}).get("RSI","N/A"),
        market_data.get("1小时",{}).get("RSI","N/A"),
        market_data.get("资金费率","N/A"),
        market_data.get("Bitget多空比",{}).get("偏向","N/A"),
        market_data.get("OI背离",{}).get("描述","无数据"),
        market_data.get("OI背离",{}).get("强度",0),
        level_action_str, la_strength,
        liq_str, ov_str, ts_str,
        market_data.get("最新消息面","")[:60],
    )
    return claude_request(prompt, max_tokens=350)

def analyze_close(close_info, trade, current_price):
    prompt = (
        "用3句口语化中文复盘这笔交易，像朋友聊天，不要标题格式：\n"
        "方向:{} 入场:${:,.0f} 平仓:${:,.0f}\n"
        "用户说:{}\n"
        "第1句：赚了还是亏了，值不值\n"
        "第2句：做对或做错了什么\n"
        "第3句：下次怎么做，X/10分"
    ).format(
        trade.get("direction", "?"),
        trade.get("entry_price", trade.get("price_at_signal", 0)),
        current_price or 0,
        close_info
    )
    return claude_request(prompt, max_tokens=150)

# ============================================================
# 信号解析
# ============================================================

def parse_signal(analysis, price, sl):
    sig = {"direction": "观望", "strength": "弱", "should_push": False, "push_reason": ""}
    for key in ["【判断】", "【市场判断】"]:
        if key in analysis:
            idx = analysis.index(key)
            chunk = analysis[idx:idx+25]
            if "做多" in chunk:
                sig["direction"] = "做多"
            elif "做空" in chunk:
                sig["direction"] = "做空"
            break
    for key in ["【强度】", "【信号强度】"]:
        if key in analysis:
            idx = analysis.index(key)
            chunk = analysis[idx:idx+15]
            if "强" in chunk:
                sig["strength"] = "强"
            elif "中" in chunk:
                sig["strength"] = "中"
            break
    for key in ["【推送】", "【是否推送】"]:
        if key in analysis:
            idx = analysis.index(key)
            chunk = analysis[idx:idx+20].split("\n")[0]
            sig["should_push"] = "是" in chunk and "否" not in chunk
            break
    for key in ["【理由】", "【推送理由】"]:
        if key in analysis:
            idx = analysis.index(key)
            sig["push_reason"] = analysis[idx+4:idx+80].split("\n")[0].strip()
            break

    if sig["direction"] == "做多":
        sig.update({"entry_low": price-200, "entry_high": price+100,
                    "stop_loss": price-sl, "target": price+TARGET_POINTS})
    elif sig["direction"] == "做空":
        sig.update({"entry_low": price-100, "entry_high": price+200,
                    "stop_loss": price+sl, "target": price-TARGET_POINTS})
    sig["stop_loss_points"] = sl
    return sig

def check_active_signal(memory, price):
    sig = memory.get("active_signal")
    if not sig:
        return None, None
    sl_pts = sig.get("stop_loss_points", STOP_LOSS_DEFAULT)
    if sig["direction"] == "做多":
        if price <= sig["stop_loss"]:
            return "止损", -sl_pts
        elif price >= sig["target"]:
            return "止盈", TARGET_POINTS
    elif sig["direction"] == "做空":
        if price >= sig["stop_loss"]:
            return "止损", -sl_pts
        elif price <= sig["target"]:
            return "止盈", TARGET_POINTS
    return None, None

def check_real_trade(memory, price):
    rt = memory.get("real_trade")
    if not rt:
        return None, None
    sl_pts = rt.get("stop_loss_points", STOP_LOSS_DEFAULT)
    if rt["direction"] == "做多":
        if price <= rt["stop_loss"]:
            return "止损", -sl_pts
        elif price >= rt["target"]:
            return "止盈", TARGET_POINTS
    elif rt["direction"] == "做空":
        if price >= rt["stop_loss"]:
            return "止损", -sl_pts
        elif price <= rt["target"]:
            return "止盈", TARGET_POINTS
    return None, None

def monitor_real_trade(memory, price):
    """监控真实持仓，价格接近关键位时提醒"""
    rt = memory.get("real_trade")
    if not rt:
        return
    direction = rt["direction"]
    ep = rt["entry_price"]
    size = rt.get("size", POSITION_DEFAULT)
    sl = rt["stop_loss"]
    targets = rt.get("targets", [])

    dist_to_sl = abs(price - sl)
    if dist_to_sl <= 100 and can_alert("sl_warning", cooldown=120):
        pnl_now = (ep - price) if direction == "做空" else (price - ep)
        msg = "<b>止损预警</b>\n"
        msg += "你的{}持仓正在接近止损位\n".format(direction)
        msg += "当前价: ${:,.0f}\n".format(price)
        msg += "止损位: ${:,.0f}（距{}点）\n".format(sl, int(dist_to_sl))
        msg += "当前浮盈: {:+.0f}点\n".format(pnl_now)
        msg += "建议：注意风险，考虑是否止损！"
        send_telegram(msg)
        log("止损预警已推送")

    for i, tg in enumerate(targets):
        tp_price = tg["price"]
        dist_to_tp = abs(price - tp_price)
        label = tg.get("label", "目标{}".format(i+1))
        hint = tg.get("hint", "考虑减仓")
        if dist_to_tp <= 150 and can_alert("tp_{}_{}".format(i, int(tp_price)), cooldown=300):
            pnl_pts = abs(tp_price - ep)
            pnl_usd = pnl_pts * size
            msg = "<b>止盈提醒 {}</b>\n".format(label)
            msg += "价格正在接近止盈目标\n"
            msg += "当前价: ${:,.0f}\n".format(price)
            msg += "目标位: ${:,.0f}（距{}点）\n".format(tp_price, int(dist_to_tp))
            msg += "预计盈利: {:+.0f}点 / +${:.0f}\n".format(pnl_pts, pnl_usd)
            msg += "建议：{}！".format(hint)
            send_telegram(msg)
            log("止盈提醒已推送: {}".format(label))

def extract_reflection(analysis):
    for key in ["【经验教训】", "【复盘总结】", "【自我反思】"]:
        if key in analysis:
            idx = analysis.index(key)
            lines = [l.strip() for l in analysis[idx+len(key):].split("\n") if l.strip()]
            return lines[0][:150] if lines else "无"
    return "无"

def update_memory(memory, analysis, signal, price, result, pnl, close_type="自动"):
    now = datetime.now().strftime("%m-%d %H:%M")
    if result and memory.get("active_signal"):
        closed = memory["active_signal"]
        closed.update({"result": result, "pnl_points": pnl,
                       "closed_time": now, "closed_price": price, "close_type": close_type})
        memory["signals"].append(closed)
        memory["active_signal"] = None
        memory["stats"]["total"] += 1
        if result == "止盈":
            memory["stats"]["wins"] += 1
        elif result == "止损":
            memory["stats"]["losses"] += 1
        else:
            memory["stats"]["manual_closes"] = memory["stats"].get("manual_closes", 0) + 1
        memory["stats"]["total_pnl_points"] += pnl
        memory["reflections"].append({
            "time": now,
            "signal": "{} {} {:+.0f}点".format(closed["direction"], result, pnl),
            "content": extract_reflection(analysis)
        })
        memory["reflections"] = memory["reflections"][-20:]

    if (signal.get("direction") in ["做多", "做空"] and
            signal.get("strength") in ["强", "中"] and
            not memory.get("active_signal")):
        memory["active_signal"] = {
            "direction": signal["direction"], "strength": signal["strength"],
            "time": now, "price_at_signal": price,
            "entry_low": signal.get("entry_low", price),
            "entry_high": signal.get("entry_high", price),
            "stop_loss": signal.get("stop_loss", 0),
            "target": signal.get("target", 0),
            "stop_loss_points": signal.get("stop_loss_points", STOP_LOSS_DEFAULT),
        }
    memory["signals"] = memory["signals"][-50:]
    save_memory(memory)

# ============================================================
# Telegram
# ============================================================

def send_telegram(message):
    import urllib3; urllib3.disable_warnings()
    try:
        r = requests.post(
            "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_BOT_TOKEN),
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
        return r.status_code == 200
    except Exception as e:
        log("TG发送失败: {}".format(e))
        return False

def parse_user_intent(text):
    open_patterns = ["开了","开仓","入场","进场","做空了","做多了","空了","多了",
                     "开空","开多","建仓","下单了","空单","多单","short","long",
                     "买入","卖出","开仓了","做空","做多","加仓","加了","加多","加空"]
    close_patterns = ["平仓","止盈","止损","跑了","出来了","平了","关仓","离场",
                      "出场","平掉","亏了","赚了","盈利","亏损","close","exit","爆仓"]

    # 问句不触发入场
    is_question = any(kw in text for kw in ["吗", "？", "?", "能不能", "可以吗", "要不要", "好吗", "行吗", "咋样"])
    is_open = any(kw in text for kw in open_patterns) and not is_question
    is_close = any(kw in text for kw in close_patterns)
    if is_open and is_close:
        is_open = False

    direction = None
    if any(kw in text for kw in ["空","做空","short","卖"]):
        direction = "做空"
    elif any(kw in text for kw in ["多","做多","long","买"]):
        direction = "做多"

    price = None
    price_match = re.search(r'(?:在|价格|@|入场价)?([1-9]\d{4,5})(?:\.\d+)?', text)
    if price_match:
        price = float(price_match.group(1))

    size = None
    size_match = re.search(r'(\d+\.?\d*)\s*(?:个|btc|BTC|张|手)', text)
    if size_match:
        size = float(size_match.group(1))

    pnl = None
    pnl_match = re.search(r'(\d+)\s*(?:点|u|usdt|USDT|美金)', text)
    if pnl_match:
        pnl = int(pnl_match.group(1))
        if any(kw in text for kw in ["亏","损","止损","爆"]):
            pnl = -pnl

    result = {"is_open": is_open, "is_close": is_close,
              "direction": direction, "price": price, "size": size, "pnl": pnl}
    log("意图识别: {}".format(result))
    return result

def handle_tg_command(text):
    text_lower = text.lower()
    intent = parse_user_intent(text)
    is_open = intent.get("is_open", False)
    is_close = intent.get("is_close", False)
    direction = intent.get("direction")
    entry_price = intent.get("price")
    size = intent.get("size") or POSITION_DEFAULT
    pnl_from_intent = intent.get("pnl")

    if is_open and not is_close:
        memory = load_memory()
        price = get_current_price()
        ep = entry_price or price
        if not direction:
            send_telegram("没有识别到方向（做多/做空），请重新发送。")
            return
        sl_pts = STOP_LOSS_DEFAULT
        sl_price = (ep + sl_pts) if direction == "做空" else (ep - sl_pts)
        target_price = (ep - TARGET_POINTS) if direction == "做空" else (ep + TARGET_POINTS)
        now = datetime.now().strftime("%m-%d %H:%M")

        # 拉取实时技术位
        targets = []
        try:
            c1h = get_klines("1H", 50)
            c4h = get_klines("4H", 20)
            ob = None  # get_orderbook不在此版本
            levels = calc_key_levels(c1h, c4h, ep, ob) if c1h and c4h else []

            if direction == "做空":
                # 止损用上方压力位
                resistance = [l for l in levels if l["price"] > ep]
                if resistance:
                    sl_price = sorted(resistance, key=lambda x: x["price"])[0]["price"] + 50
                    sl_pts = round(sl_price - ep)
                # 止盈用下方支撑位
                support = sorted([l for l in levels if l["price"] < ep],
                                  key=lambda x: x["price"], reverse=True)
                labels = ["第一目标（可减仓50%）", "第二目标（可再减30%）", "最终目标（清仓）"]
                hints = ["减仓50%锁定利润", "再减仓30%", "清仓或持仓观察"]
                for i, lv in enumerate(support[:3]):
                    targets.append({
                        "price": lv["price"],
                        "label": labels[i],
                        "hint": hints[i],
                        "sources": lv["sources"]
                    })
            else:
                # 止损用下方支撑位
                support = [l for l in levels if l["price"] < ep]
                if support:
                    sl_price = sorted(support, key=lambda x: x["price"], reverse=True)[0]["price"] - 50
                    sl_pts = round(ep - sl_price)
                # 止盈用上方压力位
                resistance = sorted([l for l in levels if l["price"] > ep],
                                     key=lambda x: x["price"])
                labels = ["第一目标（可减仓50%）", "第二目标（可再减30%）", "最终目标（清仓）"]
                hints = ["减仓50%锁定利润", "再减仓30%", "清仓或持仓观察"]
                for i, lv in enumerate(resistance[:3]):
                    targets.append({
                        "price": lv["price"],
                        "label": labels[i],
                        "hint": hints[i],
                        "sources": lv["sources"]
                    })
        except Exception as e:
            log("动态关键位计算失败: {}".format(e))

        # 如果没有技术位就用固定值
        if not targets:
            fixed_tp = (ep - TARGET_POINTS) if direction == "做空" else (ep + TARGET_POINTS)
            targets = [{"price": fixed_tp, "label": "目标位", "hint": "止盈", "sources": ["固定{}点".format(TARGET_POINTS)]}]

        memory["real_trades"] = memory.get("real_trades", [])

        # 检查是否是加仓（同币种同方向已有持仓）
        symbol = "ETH" if any(kw in text.upper() for kw in ["ETH","以太"]) else "BTC"
        existing_idx = None
        for i, t in enumerate(memory["real_trades"]):
            if t.get("symbol", "BTC") == symbol and t.get("direction") == direction:
                existing_idx = i
                break

        if existing_idx is not None:
            # 合并加仓：计算平均成本
            existing = memory["real_trades"][existing_idx]
            old_size = existing["size"]
            old_ep = existing["entry_price"]
            new_total_size = round(old_size + size, 4)
            avg_ep = round((old_ep * old_size + ep * size) / new_total_size, 2)

            # 更新持仓
            existing["size"] = new_total_size
            existing["entry_price"] = avg_ep
            existing["stop_loss"] = sl_price
            existing["target"] = targets[-1]["price"] if targets else target_price
            existing["targets"] = targets
            existing["stop_loss_points"] = sl_pts
            memory["real_trades"][existing_idx] = existing
            save_memory(memory)

            msg = "<b>{} 加仓已合并</b>\n".format(symbol)
            msg += "─────────────────────\n"
            msg += "方向: <b>{}</b>\n".format(direction)
            msg += "原仓位: {} {} @ ${:.2f}\n".format(old_size, symbol, old_ep)
            msg += "加仓: {} {} @ ${:.2f}\n".format(size, symbol, ep)
            msg += "合并后: {} {} @ ${:.2f}（均价）\n\n".format(new_total_size, symbol, avg_ep)
            msg += "止损: ${:.2f}（{}点）\n".format(sl_price, sl_pts)
            if targets:
                msg += "目标: ${:.2f}\n".format(targets[0]["price"])
            msg += "<i>平仓后发：{}止盈了 或 {}止损了</i>".format(symbol, symbol)
            send_telegram(msg)
            log("加仓合并: {} {} {} @ ${:.2f} 均价${:.2f}".format(symbol, direction, new_total_size, ep, avg_ep))
            return
        
        # 检测币种
        symbol = "ETH" if any(kw in text.upper() for kw in ["ETH","以太"]) else "BTC"

        new_trade = {
            "symbol": symbol,
            "direction": direction, "entry_price": ep, "size": size,
            "stop_loss": sl_price, "target": targets[-1]["price"] if targets else target_price,
            "targets": targets, "stop_loss_points": sl_pts,
            "time": now, "status": "持仓中"
        }
        memory["real_trades"].append(new_trade)
        # 兼容旧版
        memory["real_trade"] = new_trade
        save_memory(memory)

        # 发送完整交易管理方案
        msg = "<b>{} 入场已记录</b>\n".format(symbol)
        msg += "─────────────────────\n"
        msg += "方向: <b>{}</b> | 仓位: {} {}\n".format(direction, size, symbol)
        msg += "入场价: ${:,.2f}\n\n".format(ep)
        msg += "<b>止损位</b>\n"
        msg += "破 ${:,.2f} 止损（{}点）\n\n".format(sl_price, sl_pts)
        msg += "<b>止盈目标</b>\n"
        for tg in targets:
            pnl_pts = abs(tg["price"] - ep)
            pnl_usd = pnl_pts * size
            src = "、".join(tg["sources"][:2]) if tg.get("sources") else ""
            msg += "{}: ${:,.2f} (+{:.0f}点 / +${:.0f})\n".format(
                tg["label"], tg["price"], pnl_pts, pnl_usd)
            if src:
                msg += "  ({})\n".format(src)
        msg += "─────────────────────\n"
        msg += "<i>价格接近各位置时龙虾会提醒你</i>\n"
        msg += "<i>平仓后发：{}止盈了 或 {}止损了</i>".format(symbol, symbol)
        send_telegram(msg)
        log("入场记录: {} {} {} @ ${:,.2f}".format(symbol, direction, size, ep))

    elif is_close:
        memory = load_memory()
        price = get_current_price()
        trades = memory.get("real_trades", [])
        rt = memory.get("real_trade")
        sig = memory.get("active_signal")

        # 检测要平仓的币种
        symbol = "ETH" if any(kw in text.upper() for kw in ["ETH","以太"]) else "BTC"

        # 找到对应的持仓
        trade = None
        trade_idx = None
        for i, t in enumerate(trades):
            if t.get("symbol", "BTC") == symbol:
                trade = t
                trade_idx = i
                break

        if not trade:
            trade = rt or sig
            symbol = "BTC"

        if not trade and not sig:
            send_telegram("当前没有{}记录中的交易。".format(symbol))
            return

        # 检测是否是减仓（分批止盈）
        reduce_keywords = ["减仓", "平了一半", "平了一部分", "部分止盈", "减了"]
        is_partial = any(kw in text for kw in reduce_keywords)

        # 提取减仓数量
        reduce_size = None
        if is_partial:
            size_match = re.search(r'(\d+\.?\d*)\s*(?:个|btc|BTC|eth|ETH)', text)
            if size_match:
                reduce_size = float(size_match.group(1))

        # 提取平仓价格
        close_price = None
        close_price_match = re.search(r'(?:在|@|价格)?(\d{4,6}\.?\d*)', text)
        if close_price_match:
            close_price = float(close_price_match.group(1))

        pnl = pnl_from_intent or 0
        if not pnl:
            ep = trade.get("entry_price", trade.get("price_at_signal", 0))
            actual_close = close_price or price or ep
            calc_size = reduce_size or trade.get("size", POSITION_DEFAULT)
            if ep and actual_close:
                if trade.get("direction") == "做空":
                    pnl = round(ep - actual_close)
                else:
                    pnl = round(actual_close - ep)
            if any(kw in text for kw in ["亏","损","止损","爆"]) and pnl > 0:
                pnl = -pnl

        # 如果是分批止盈，更新剩余仓位
        if is_partial and reduce_size and trade_idx is not None:
            remain_size = round(trade.get("size", 0) - reduce_size, 4)
            pnl_usd = abs(pnl) * reduce_size
            if remain_size > 0:
                memory["real_trades"][trade_idx]["size"] = remain_size
                save_memory(memory)
                # 更新统计
                memory["stats"]["total_pnl_usdt"] = memory["stats"].get("total_pnl_usdt", 0) + (pnl_usd if pnl >= 0 else -pnl_usd)
                save_memory(memory)
                msg = "V <b>{} 分批止盈</b>\n".format(symbol)
                msg += "减仓: {} {} @ ${:.2f}\n".format(reduce_size, symbol, close_price or price or 0)
                msg += "锁定: {:+.0f}点 / {:+.0f} USDT\n".format(pnl, pnl_usd)
                msg += "剩余仓位: {} {} 继续持有".format(remain_size, symbol)
                send_telegram(msg)
                log("用户分批止盈: {} {} {} 剩余{}".format(symbol, pnl, pnl_usd, remain_size))
                return

        send_telegram("收到，正在复盘...")
        reflection = analyze_close(text, trade, price)
        result_type = "手动止盈" if pnl >= 0 else "手动止损"
        pnl_usd = abs(pnl) * trade.get("size", POSITION_DEFAULT)

        # 更新统计
        memory["stats"]["total"] += 1
        if pnl >= 0:
            memory["stats"]["wins"] += 1
        else:
            memory["stats"]["losses"] += 1
        memory["stats"]["total_pnl_points"] += pnl
        memory["stats"]["manual_closes"] = memory["stats"].get("manual_closes", 0) + 1

        # 更新USDT盈亏统计
        memory["stats"]["total_pnl_usdt"] = memory["stats"].get("total_pnl_usdt", 0) + (pnl_usd if pnl >= 0 else -pnl_usd)

        # 移除已平仓的交易
        if trade_idx is not None:
            memory["real_trades"].pop(trade_idx)
        memory["real_trade"] = None
        save_memory(memory)

        emoji = "V" if pnl >= 0 else "X"
        msg = "{} <b>{} 交易复盘</b>\n".format(emoji, symbol)
        msg += "方向: {} | {} {:+.0f}点 / {:+.0f} USDT\n\n".format(
            trade.get("direction", "?"), result_type, pnl, pnl_usd)
        msg += reflection or "复盘生成失败，已记录。"
        msg += "\n\n<i>— 龙虾交易大脑</i>"
        send_telegram(msg)

    elif any(kw in text for kw in ["计划", "等什么", "等哪里", "在等", "入场计划"]):
        # 量化计划大白话
        price = get_current_price()
        try:
            c4h = get_klines("4H", 20)
            c1h = get_klines("1H", 50)
            closes_4h = [c["close"] for c in c4h]
            rsi_4h = calc_rsi(closes_4h)
            ema20 = sum(closes_4h[-20:]) / 20
            highs = [c["high"] for c in c4h]
            lows = [c["low"] for c in c4h]
            high = max(highs)
            low = min(lows)
            diff = high - low
            fibs = [
                ("F38.2%", round(high - diff * 0.382)),
                ("F50%",   round(high - diff * 0.5)),
                ("F61.8%", round(high - diff * 0.618)),
                ("F78.6%", round(high - diff * 0.786)),
            ]
            closes_1h = [c["close"] for c in c1h]
            bb_u, bb_m, bb_l = calc_bollinger(closes_1h)
            bb_width = round((bb_u - bb_l) / bb_m * 100, 1) if bb_u and bb_l and bb_m else 0

            msg = "🦞 <b>龙虾量化计划</b>\n"
            msg += "─────────────────────\n"
            msg += "当前价格: ${:,.0f}\n".format(price)
            msg += "4H RSI: {:.0f} | EMA20: ${:,.0f}\n".format(rsi_4h, ema20)
            msg += "布林带宽度: {:.1f}%（需要>3%才开仓）\n\n".format(bb_width)

            if rsi_4h > 55 and price > ema20:
                msg += "📈 <b>趋势：做多方向</b>\n"
                msg += "等待价格回调到以下Fib支撑位附近再做多：\n"
                for name, fp in fibs:
                    dist = int(fp - price)
                    if dist < 0:
                        msg += "  {} ${:,.0f}（需跌{:.0f}点）\n".format(name, fp, abs(dist))
            elif rsi_4h < 45 and price < ema20:
                msg += "📉 <b>趋势：做空方向</b>\n"
                msg += "等待价格反弹到以下Fib压力位附近再做空：\n"
                for name, fp in fibs:
                    dist = int(fp - price)
                    if dist > 0:
                        msg += "  {} ${:,.0f}（需涨{:.0f}点）\n".format(name, fp, dist)
            else:
                msg += "⏳ <b>趋势：中性观望</b>\n"
                msg += "RSI{:.0f}在45-55之间，方向不明\n".format(rsi_4h)
                msg += "等RSI突破55做多，或跌破45做空\n\n"
                msg += "关键Fib位参考：\n"
                for name, fp in fibs:
                    dist = int(fp - price)
                    msg += "  {} ${:,.0f}（{:+.0f}点）\n".format(name, fp, dist)

            msg += "\n止损：200点 | 止盈：2000点+"
            send_telegram(msg)
        except Exception as e:
            send_telegram("计划获取失败: {}".format(str(e)[:50]))

    elif "状态" in text or "status" in text_lower:
        memory = load_memory()
        trades = memory.get("real_trades", [])
        bt = memory.get("bot_trade")
        price_btc = get_current_price()
        stats = memory["stats"]
        bot_stats = memory.get("bot_stats", {"total": 0, "wins": 0, "losses": 0,
                                              "total_pnl_points": 0, "capital": 3300})

        INITIAL_CAPITAL = 3300.0
        user_pnl_usd = stats.get("total_pnl_usdt", stats["total_pnl_points"] * POSITION_DEFAULT)
        user_total = INITIAL_CAPITAL + user_pnl_usd
        user_busted = user_total <= 0
        bot_pnl_usd = bot_stats.get("total_pnl_usdt", bot_stats["total_pnl_points"] * POSITION_DEFAULT)
        bot_total = INITIAL_CAPITAL + bot_pnl_usd
        bot_busted = bot_total <= 0

        price_str = "${:,.0f}".format(price_btc) if price_btc else "获取失败"
        try:
            r_eth = requests.get("https://www.okx.com/api/v5/market/ticker",
                                params={"instId": "ETH-USDT"}, timeout=5)
            eth_str = "${:,.2f}".format(float(r_eth.json()["data"][0]["last"]))
        except:
            eth_str = "N/A"
        msg = "<b>当前状态</b>\n"
        msg += "BTC: {} | ETH: {}\n".format(price_str, eth_str)
        msg += "─────────────────────\n"

        # 你的账户
        user_title = "<b>你的账户</b>" if not user_busted else "<b>你的账户 已爆仓</b>"
        msg += "{}\n".format(user_title)
        msg += "初始资金: $3,300 | 总金额: ${:,.0f}\n".format(max(user_total, 0))
        msg += "累计盈亏: {:+.0f} USDT\n".format(user_pnl_usd)
        msg += "战绩: {}胜{}负\n".format(stats["wins"], stats["losses"])

        if trades and not user_busted:
            msg += "\n<b>持仓中：</b>\n"
            for t in trades:
                sym = t.get("symbol", "BTC")
                # 获取对应价格
                if sym == "ETH":
                    try:
                        r = requests.get("https://www.okx.com/api/v5/market/ticker",
                                        params={"instId": "ETH-USDT"}, timeout=5)
                        cur_price = float(r.json()["data"][0]["last"])
                    except:
                        cur_price = t["entry_price"]
                else:
                    cur_price = price_btc or t["entry_price"]

                if t["direction"] == "做空":
                    pnl_now = round(t["entry_price"] - cur_price, 2)
                else:
                    pnl_now = round(cur_price - t["entry_price"], 2)
                pnl_now_usd = pnl_now * t["size"]
                msg += "{} {} {}个 @ ${:.2f}\n".format(
                    sym, t["direction"], t["size"], t["entry_price"])
                msg += "浮盈: {:+.2f}点 / {:+.0f} USDT\n".format(pnl_now, pnl_now_usd)
                msg += "止损: ${:.2f} | 目标: ${:.2f}\n".format(t["stop_loss"], t["target"])
        elif not user_busted:
            msg += "无持仓，监测中...\n"

        msg += "─────────────────────\n"

        # 龙虾账户
        bot_title = "<b>龙虾账户</b>" if not bot_busted else "<b>龙虾账户 已爆仓</b>"
        msg += "{}\n".format(bot_title)
        msg += "初始资金: $3,300 | 总金额: ${:,.0f}\n".format(max(bot_total, 0))
        msg += "累计盈亏: {:+.0f} USDT\n".format(bot_pnl_usd)
        msg += "战绩: {}胜{}负\n".format(bot_stats["wins"], bot_stats["losses"])

        if bt and price_btc and not bot_busted:
            if bt["direction"] == "做空":
                bt_pnl_now = round(bt["entry_price"] - price_btc)
            else:
                bt_pnl_now = round(price_btc - bt["entry_price"])
            bt_pnl_usd = bt_pnl_now * bt["size"]
            msg += "持仓: BTC {} {}个 @ ${:,.0f}\n".format(
                bt["direction"], bt["size"], bt["entry_price"])
            msg += "浮盈: {:+.0f}点 / {:+.0f} USDT\n".format(bt_pnl_now, bt_pnl_usd)
            msg += "止损: ${:,.0f} | 目标: ${:,.0f}".format(bt["stop_loss"], bt["target"])
        elif not bot_busted:
            msg += "无持仓，等待信号..."
        send_telegram(msg)

    else:
        # 主动问龙虾行情分析
        query_keywords = ["分析", "看多", "看空", "能开多", "能开空", "可以开多", "可以开空",
                         "可以多", "可以空", "能多", "能空", "现在怎么样", "行情", "怎么看",
                         "信号", "做多吗", "做空吗", "多还是空", "该做多", "该做空", "建议",
                         "现在可以", "能做", "适合做", "适合开"]
        is_query = any(kw in text for kw in query_keywords)

        if is_query:
            send_telegram("收到，龙虾马上分析给你...")
            log("收到主动查询，触发即时分析")
            try:
                price = get_current_price()
                market_data = collect_data()
                memory = load_memory()
                sl = market_data.get("动态止损点数", STOP_LOSS_DEFAULT)
                analysis = analyze_main(market_data, format_memory_for_claude(memory))
                if analysis:
                    signal = parse_signal(analysis, price, sl)
                    memory2 = load_memory()
                    last_price = memory2.get("last_push_price", price)
                    last_dir = memory2.get("last_push_direction", "")
                    price_move = abs(price - last_price)
                    is_opposite = signal["direction"] != last_dir and last_dir != ""
                    # 即时分析也遵守冷却：反向需500点，同向需30分钟
                    if is_opposite and price_move < 500:
                        send_telegram("<b>龙虾分析</b>\n方向与上次相反且价格波动不足500点，仅供参考\n\n" + format_push_message(analysis, price, signal))
                    elif not can_alert("signal_push", cooldown=1800):
                        send_telegram("<b>龙虾分析</b>\n推送冷却中，仅供参考\n\n" + format_push_message(analysis, price, signal))
                    else:
                        memory2["last_push_price"] = price
                        memory2["last_push_direction"] = signal["direction"]
                        save_memory(memory2)
                        send_telegram(format_push_message(analysis, price, signal))
                else:
                    send_telegram("分析失败，请稍后再试。")
            except Exception as e:
                log("即时分析失败: {}".format(e))
                send_telegram("分析出错了，请稍后再试。")

def format_push_message(analysis, price, signal, result=None, pnl=0):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    sl = signal.get("stop_loss_points", STOP_LOSS_DEFAULT)
    rr = round(TARGET_POINTS/sl, 1)

    if result:
        emoji = "V" if "止盈" in result else "X"
        pnl_usd = abs(pnl) * POSITION_DEFAULT
        msg = "{} <b>BTC 信号结果</b>\n".format(emoji)
        msg += "时间: {} | 价格: ${:,.0f}\n".format(now, price)
        msg += "结果: <b>{} {:+.0f}点 / ${:.0f}</b>\n\n".format(result, pnl, pnl_usd)
        msg += "{}\n\n<i>— 龙虾交易大脑</i>".format(analysis)
        return msg

    direction = signal["direction"]
    strength = signal["strength"]

    if direction == "做空":
        header = "🔴🔴🔴 <b>做  空</b> 🔴🔴🔴"
    elif direction == "做多":
        header = "🟢🟢🟢 <b>做  多</b> 🟢🟢🟢"
    else:
        header = "👀 <b>观  望</b> 👀"

    s_emoji = "强" if strength == "强" else "中" if strength == "中" else "弱"
    push_reason = signal.get("push_reason", "")

    msg = "{}\n".format(header)
    msg += "─────────────────────\n"
    msg += "时间: {} | 价格: ${:,.0f}\n".format(now, price)
    msg += "信号强度: {}\n".format(s_emoji)
    if direction != "观望":
        msg += "止损: {}点 | 目标: {}点 | 风报比: 1:{}\n".format(sl, TARGET_POINTS, rr)
    if push_reason:
        msg += "理由: {}\n".format(push_reason)
    msg += "─────────────────────\n\n"
    msg += "{}\n\n".format(analysis)
    if direction != "观望":
        msg += "<i>入场后发：我在{}开了X个BTC{}</i>\n".format(price, direction[1:])
    msg += "<i>— 龙虾交易大脑</i>"
    return msg

def poll_telegram_commands():
    last_update_id = 0
    update_file = os.path.join(MEMORY_DIR, "last_update_id.txt")
    if os.path.exists(update_file):
        with open(update_file) as f:
            try:
                last_update_id = int(f.read().strip())
            except:
                pass
    while True:
        try:
            r = requests.get(
                "https://api.telegram.org/bot{}/getUpdates".format(TELEGRAM_BOT_TOKEN),
                params={"offset": last_update_id + 1, "timeout": 30},
                timeout=35
            )
            for update in r.json().get("result", []):
                last_update_id = update["update_id"]
                msg = update.get("message", {})
                text = msg.get("text", "").strip()
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if chat_id == TELEGRAM_CHAT_ID and text:
                    log("收到TG消息: {}".format(text))
                    t = threading.Thread(target=handle_tg_command, args=(text,), daemon=True)
                    t.start()
            with open(update_file, "w") as f:
                f.write(str(last_update_id))
        except Exception as e:
            log("TG轮询失败: {}".format(e))
        time.sleep(5)

# ============================================================
# 主循环
# ============================================================

def run_analysis():
    os.environ["https_proxy"] = "http://127.0.0.1:7897"
    os.environ["http_proxy"] = "http://127.0.0.1:7897"

    memory = load_memory()
    price = get_current_price()
    if not price:
        log("获取价格失败，跳过")
        return

    log("BTC: ${:,.0f}".format(price))

    # 监控持仓关键位
    monitor_real_trade(memory, price)

    # 检查龙虾追踪信号
    result, pnl = check_active_signal(memory, price)
    if result:
        log("信号触发: {} {:+.0f}点".format(result, pnl))

    # 检查你的真实持仓
    rt_result, rt_pnl = check_real_trade(memory, price)
    if rt_result:
        log("你的持仓触发: {} {:+.0f}点".format(rt_result, rt_pnl))
        rt = memory["real_trade"]
        pnl_usd = abs(rt_pnl) * rt.get("size", POSITION_DEFAULT)
        emoji = "V" if rt_result == "止盈" else "X"
        msg = "{} <b>你的持仓已触发{}</b>\n".format(emoji, rt_result)
        msg += "方向: {} | {:+.0f}点 / ${:.0f}\n".format(rt["direction"], rt_pnl, pnl_usd)
        msg += "入场: ${:,.0f} | 当前: ${:,.0f}".format(rt["entry_price"], price)
        send_telegram(msg)
        memory["real_trade"] = None
        save_memory(memory)

    market_data = collect_data()
    sl = market_data.get("动态止损点数", STOP_LOSS_DEFAULT)
    log("动态止损: {}点".format(sl))

    # ── 止盈止损前置检查（不依赖Claude）──────────
    _bot_trade = memory.get("bot_trade")
    if _bot_trade and price:
        _direction = _bot_trade["direction"]
        _ep = _bot_trade["entry_price"]
        _sl = _bot_trade["stop_loss"]
        _sl_pts = _bot_trade.get("stop_loss_points", 200)
        _size = _bot_trade.get("size", 0.1)
        _targets = _bot_trade.get("targets", [])
        _pnl = round(_ep - price) if _direction == "做空" else round(price - _ep)

        # 止损检查
        _hit_sl = (_direction == "做空" and price >= _sl) or (_direction == "做多" and price <= _sl)
        if _hit_sl:
            _pnl_u = round(-_sl_pts * _size, 2)
            memory["bot_trade"] = None
            bs = memory.get("bot_stats", {})
            bs["losses"] = bs.get("losses", 0) + 1
            bs["total"] = bs.get("total", 0) + 1
            bs["total_pnl_points"] = bs.get("total_pnl_points", 0) - _sl_pts
            bs["total_pnl_usdt"] = bs.get("total_pnl_usdt", 0) + _pnl_u
            memory["bot_stats"] = bs
            save_memory(memory)
            send_telegram("❌ <b>龙虾止损</b>\n方向:{} 亏损:{:.0f}点 / {:.1f}U".format(_direction, _sl_pts, _pnl_u))
            log("龙虾止损: {} -{:.0f}点".format(_direction, _sl_pts))
        else:
            # 移动止损跟踪止盈（到达2000点后跟踪，回撤200点出场）
            _pnl_now = round(_ep - price) if _direction == "做空" else round(price - _ep)

            # 更新最高浮盈
            _peak = _bot_trade.get("peak_pnl", 0)
            if _pnl_now > _peak:
                memory["bot_trade"]["peak_pnl"] = _pnl_now
                save_memory(memory)
                _peak = _pnl_now

            # 到达2000点后启动移动止损跟踪
            if _peak >= 2000:
                _trail_sl = _peak - 200  # 从峰值回撤200点出场
                if _pnl_now <= _trail_sl:
                    _tp_pts = round(_pnl_now)
                    _pnl_u = round(_tp_pts * _size, 2)
                    memory["bot_trade"] = None
                    bs = memory.get("bot_stats", {})
                    bs["wins"] = bs.get("wins", 0) + 1
                    bs["total"] = bs.get("total", 0) + 1
                    bs["total_pnl_points"] = bs.get("total_pnl_points", 0) + _tp_pts
                    bs["total_pnl_usdt"] = bs.get("total_pnl_usdt", 0) + _pnl_u
                    memory["bot_stats"] = bs
                    save_memory(memory)
                    send_telegram("✅ <b>龙虾移动止盈</b>\n方向:{} 峰值:{:.0f}点 出场:{:.0f}点 / +{:.1f}U".format(_direction, _peak, _tp_pts, _pnl_u))
                    log("龙虾移动止盈: {} 峰值{}点 出场{}点".format(_direction, _peak, _tp_pts))
            else:
                # 未到2000点，用固定目标位止盈
                for _tg in _targets:
                    _hit_tp = (_direction == "做多" and price >= _tg["price"]) or (_direction == "做空" and price <= _tg["price"])
                    if _hit_tp:
                        _tp_pts = round(abs(_tg["price"] - _ep))
                        _pnl_u = round(_tp_pts * _size, 2)
                        memory["bot_trade"] = None
                        bs = memory.get("bot_stats", {})
                        bs["wins"] = bs.get("wins", 0) + 1
                        bs["total"] = bs.get("total", 0) + 1
                        bs["total_pnl_points"] = bs.get("total_pnl_points", 0) + _tp_pts
                        bs["total_pnl_usdt"] = bs.get("total_pnl_usdt", 0) + _pnl_u
                        memory["bot_stats"] = bs
                        save_memory(memory)
                        send_telegram("✅ <b>龙虾止盈</b>\n方向:{} 盈利:+{:.0f}点 / +{:.1f}U".format(_direction, _tp_pts, _pnl_u))
                        log("龙虾止盈: {} +{:.0f}点".format(_direction, _tp_pts))
                        break

    # ── 加仓持仓检查（bot_trade2）──────────────────
    _bt2 = memory.get("bot_trade2")
    if _bt2 and price:
        _d2 = _bt2["direction"]
        _ep2 = _bt2["entry_price"]
        _sl2 = _bt2["stop_loss"]
        _tp2 = _bt2["target"]
        _sz2 = _bt2["size"]
        if _d2 == "做空" and price >= _sl2:
            _pu2 = round((_ep2 - _sl2) * _sz2, 2)
            memory["bot_trade2"] = None
            save_memory(memory)
            send_telegram("❌ <b>加仓止损</b>\n方向:{} 亏损:{:.0f}点 / {:.1f}U".format(_d2, abs(_ep2-_sl2), abs(_pu2)))
            log("加仓止损: {}".format(_d2))
        elif _d2 == "做多" and price <= _sl2:
            _pu2 = round((_sl2 - _ep2) * _sz2, 2)
            memory["bot_trade2"] = None
            save_memory(memory)
            send_telegram("❌ <b>加仓止损</b>\n方向:{} 亏损:{:.0f}点 / {:.1f}U".format(_d2, abs(_ep2-_sl2), abs(_pu2)))
            log("加仓止损: {}".format(_d2))
        elif _d2 == "做多" and price >= _tp2:
            _pu2 = round((_tp2 - _ep2) * _sz2, 2)
            memory["bot_trade2"] = None
            save_memory(memory)
            send_telegram("✅ <b>加仓止盈</b>\n方向:{} 盈利:+{:.0f}点 / +{:.1f}U".format(_d2, _tp2-_ep2, _pu2))
            log("加仓止盈: {} +{:.0f}点".format(_d2, _tp2-_ep2))
        elif _d2 == "做空" and price <= _tp2:
            _pu2 = round((_ep2 - _tp2) * _sz2, 2)
            memory["bot_trade2"] = None
            save_memory(memory)
            send_telegram("✅ <b>加仓止盈</b>\n方向:{} 盈利:+{:.0f}点 / +{:.1f}U".format(_d2, _ep2-_tp2, _pu2))
            log("加仓止盈: {} +{:.0f}点".format(_d2, _ep2-_tp2))

    # ── 纯量化模式入场（不依赖Claude）──────────────
    qs = market_data.get("量化信号", {})
    qs_dir = qs.get("direction")
    log("量化信号: {} | {}".format(qs_dir or "观望", qs.get("reason", "")[:60]))

    import time as _time
    recent_losses = [s for s in memory.get("signals", [])[-3:] if s.get("pnl_points", 0) < 0]
    fuse_until = memory.get("fuse_until", 0)
    if fuse_until > _time.time():
        hours_left = round((fuse_until - _time.time()) / 3600, 1)
        log("熔断保护中，还剩{}小时".format(hours_left))
        qs_dir = None
    elif len(recent_losses) >= 3:
        memory["fuse_until"] = _time.time() + 24 * 3600
        save_memory(memory)
        log("连续止损3次，触发熔断24小时")
        send_telegram("🛑 <b>龙虾熔断保护</b>\n连续止损3次，暂停开仓24小时")
        qs_dir = None

    if qs_dir in ["做多", "做空"] and not memory.get("bot_trade"):
        direction = qs_dir
        sl_pts = 200
        price_now = price
        if direction == "做多":
            bot_sl = round(price_now - sl_pts, 0)
            bot_tp = round(price_now + 2000, 0)
            bot_targets = [{"price": bot_tp, "ratio": 1.0, "label": "止盈+2000点"}]
        else:
            bot_sl = round(price_now + sl_pts, 0)
            bot_tp = round(price_now - 2000, 0)
            bot_targets = [{"price": bot_tp, "ratio": 1.0, "label": "止盈+2000点"}]
        bs = memory.get("bot_stats", {"total":0,"wins":0,"losses":0,"total_pnl_points":0,"total_pnl_usdt":0})
        current_capital = 3300 + bs.get("total_pnl_usdt", 0)
        position_size = round(current_capital * 0.02 / sl_pts, 3)
        position_size = max(0.01, min(0.1, position_size))
        memory["bot_trade"] = {
            "direction": direction, "entry_price": price_now,
            "size": position_size, "stop_loss": bot_sl, "target": bot_tp,
            "targets": bot_targets, "stop_loss_points": sl_pts,
            "time": datetime.now().strftime("%m-%d %H:%M"),
            "open_reason": "量化信号: {}".format(qs.get("reason","")[:60]),
        }
        save_memory(memory)
        msg = "🦞 <b>龙虾量化开仓</b>\n"
        msg += "方向: <b>{}</b> @ ${:,.0f}\n".format(direction, price_now)
        msg += "止损: ${:,.0f}（{}点）\n".format(bot_sl, sl_pts)
        msg += "目标: ${:,.0f}（+2000点）\n".format(bot_tp)
        msg += "仓位: {}BTC | 资金: ${:,.0f}".format(position_size, current_capital)
        send_telegram(msg)
        log("量化开仓: {} {}BTC @ ${:,.0f}".format(direction, position_size, price_now))

    log("Claude思考中...")
    analysis = analyze_main(market_data, format_memory_for_claude(memory))
    if not analysis:
        return

    log("\n{}\n{}\n{}".format("="*50, analysis, "="*50))
    signal = parse_signal(analysis, price, sl)
    log("判断: {} | 强度: {} | 推送: {}".format(
        signal["direction"], signal["strength"], signal["should_push"]))

    update_memory(memory, analysis, signal, price, result, pnl)

    # 每5笔交易触发参数自我优化
    try:
        param_review(memory, market_data)
        memory = load_memory()  # 重新加载，参数可能已更新
    except Exception as e:
        log("参数复盘异常: {}".format(e))

    # 龙虾自动模拟开仓（使用实时技术位 + 动态仓位）
    # 检查是否有待执行的反手信号
    pending_reverse = memory.get("pending_reverse")
    if pending_reverse and not memory.get("bot_trade"):
        expire = pending_reverse.get("expire_after", 0)
        if expire > 0:
            # 如果新信号方向和反手方向一致，优先执行
            if signal["direction"] == pending_reverse["direction"]:
                log("反手信号确认，方向一致: {}".format(pending_reverse["direction"]))
                signal["strength"] = "强"  # 反手信号提升为强
            memory["pending_reverse"]["expire_after"] = expire - 1
            if expire <= 1:
                del memory["pending_reverse"]
            save_memory(memory)

    if signal["direction"] in ["做多", "做空"] and signal["strength"] in ["强", "中"]:
        if not memory.get("bot_trade"):

            # 市场状态不适合 → 拒绝入场
            if not should_trade:
                log("龙虾拒绝入场: 市场状态 {} — {}".format(mkt_state, mkt_desc))
                if can_alert("refuse_trade", cooldown=1800):
                    send_telegram("👀 <b>龙虾决定观望</b>\n市场状态: {}\n{}\n等待更好机会...".format(mkt_state, mkt_desc))
                return

            # 单边趋势熔断：逆势方向直接拒绝
            if trend_bias == "空" and signal["direction"] == "做多":
                log("单边下跌熔断：拒绝做多，当前{}".format(mkt_state))
                if can_alert("one_sided_block", cooldown=1800):
                    send_telegram("🚫 <b>单边趋势熔断</b>\n市场: {} — {}\n信号做多被拒绝，单边下跌中逆势做多胜率极低\n等待趋势反转信号...".format(mkt_state, mkt_desc))
                return
            if trend_bias == "多" and signal["direction"] == "做空":
                log("单边上涨熔断：拒绝做空，当前{}".format(mkt_state))
                if can_alert("one_sided_block", cooldown=1800):
                    send_telegram("🚫 <b>单边趋势熔断</b>\n市场: {} — {}\n信号做空被拒绝，单边上涨中逆势做空胜率极低\n等待趋势反转信号...".format(mkt_state, mkt_desc))
                return

            direction = signal["direction"]
            sl_pts = signal.get("stop_loss_points", STOP_LOSS_DEFAULT)

            # 单边趋势时止损自动放宽（避免被扫）
            if trend_bias is not None:
                dp_temp = get_params(memory)
                sl_pts = max(sl_pts, int(dp_temp.get("stop_loss_max", 800) * 1.2))
                log("单边趋势止损放宽至: {}点".format(sl_pts))

            # 基础数据
            bs = memory.get("bot_stats", {"total": 0, "wins": 0, "losses": 0,
                                          "total_pnl_points": 0, "total_pnl_usdt": 0})
            current_capital = 3300 + bs.get("total_pnl_usdt", bs["total_pnl_points"] * POSITION_DEFAULT)
            goal = memory.get("goal", {})
            round_target = goal.get("round_target", 6600)
            remaining = max(round_target - current_capital, 0)
            recent_losses = sum(1 for s in memory.get("signals", [])[-3:] if s.get("pnl_points", 0) < 0)

            # 趋势判断（结合新的trend_bias）
            daily_candles = market_data.get("日线", {})
            daily_rsi = daily_candles.get("RSI", 50) or 50
            if trend_bias == "空":
                trend = "单边下跌"
                is_counter_trend = (direction == "做多")
            elif trend_bias == "多":
                trend = "单边上涨"
                is_counter_trend = (direction == "做空")
            elif daily_rsi > 55:
                trend = "上升趋势"
                is_counter_trend = (direction == "做空")
            elif daily_rsi < 45:
                trend = "下降趋势"
                is_counter_trend = (direction == "做多")
            else:
                trend = "震荡"
                is_counter_trend = False
            trend_desc = "逆势（{}，当前{}）".format(trend, direction) if is_counter_trend else \
                         "顺势（{}，当前{}）".format(trend, direction)

            # 读取动态参数
            dp = get_params(memory)

            # 用户交易记录摘要（供龙虾参考）
            user_signals = memory.get("signals", [])[-10:]
            user_summary = ""
            if user_signals:
                u_wins = sum(1 for s in user_signals if s.get("pnl_points", 0) > 0)
                u_total = len(user_signals)
                u_pnl = sum(s.get("pnl_points", 0) for s in user_signals)
                user_summary = "用户最近{}笔: {}胜{}负 累计{:+.0f}点".format(
                    u_total, u_wins, u_total - u_wins, u_pnl)

            # 让Claude自主决定仓位和策略（顺势加仓，亏损不加仓）
            strength = signal.get("strength", "中")
            ret_data = market_data.get("回撤分析", {})
            ret_phase = ret_data.get("phase", "无明显趋势")
            ret_signal = ret_data.get("signal", "")
            near_key = ret_data.get("near_key_level", False)

            # 连续亏损保护：连亏2次以上强制最小仓位
            if recent_losses >= 2:
                position_size = 0.05
                position_reason = "连续亏损{}次，强制保守仓位0.05BTC".format(recent_losses)
                log("连续亏损保护: 仓位降至0.05BTC")
            else:
                size_prompt = (
                    "你是BTC合约交易员，核心原则：首仓要小，方向确认盈利后才加仓；亏损绝不加仓。\n"
                    "当前信号: {} {} | 趋势状态: {}\n"
                    "回撤分析: {} | 靠近关键位: {}\n"
                    "资金: ${:.0f} | 翻倍目标: ${:.0f}\n"
                    "最近亏损次数: {}次 | {}\n"
                    "参数参考: 强信号{}BTC 普通{}BTC 逆势{}BTC\n\n"
                    "仓位决策规则：\n"
                    "- 首次开仓：最大0.1BTC（不管多强的信号，首仓要小）\n"
                    "- 如果方向确认、盈利中：可以加仓到0.15-0.2BTC\n"
                    "- 逆势交易：最大0.05BTC，快进快出\n"
                    "- 连续亏损：仓位减半\n"
                    "- 在斐波那契关键位入场：仓位可以稍大（+0.02BTC）\n\n"
                    "只回答：仓位X BTC，理由一句话"
                ).format(
                    direction, strength, trend_desc,
                    ret_phase, "是" if near_key else "否",
                    current_capital, round_target,
                    recent_losses, user_summary,
                    dp["position_strong"], dp["position_normal"], dp["position_counter"],
                )

                position_size = 0.1  # 默认首仓0.1
                position_reason = "默认首仓0.1BTC"
                counter_trend_target = dp["counter_trend_target"]

                try:
                    size_result = claude_request(size_prompt, max_tokens=60)
                    if size_result:
                        log("龙虾仓位决策: {}".format(size_result.strip()[:80]))
                        nums = re.findall(r'\d+\.?\d*', size_result)
                        if nums:
                            suggested = float(nums[0])
                            # 强制上限：首仓不超过0.15，逆势不超过0.06
                            if is_counter_trend:
                                position_size = max(0.03, min(0.06, suggested))
                            else:
                                position_size = max(0.05, min(0.15, suggested))
                        position_reason = size_result.strip()[:60]
                except Exception as e:
                    log("仓位决策失败: {}".format(e))
                    if is_counter_trend:
                        position_size = 0.05
                        position_reason = "逆势保守仓位"
                    elif strength == "强" and near_key:
                        position_size = 0.12
                        position_reason = "强信号+关键位共振"
                    else:
                        position_size = 0.1
                        position_reason = "标准首仓"

            log("仓位决策: {}BTC | {} | {}".format(position_size, trend_desc, position_reason))

            # 关键位行为信号 → 影响止损止盈
            la = market_data.get("关键位行为", {})
            la_action = la.get("action")
            la_sl_hint = la.get("sl_hint")
            la_tp_hint = la.get("tp_hint")
            la_strength = la.get("strength", 0)

            # 如果关键位行为与开仓方向一致，用关键位提示的止损
            use_level_sl = (
                la_action in ["breakout_up", "rejection_up"] and direction == "做多" and la_sl_hint
            ) or (
                la_action in ["breakout_down", "rejection_down"] and direction == "做空" and la_sl_hint
            )

            # 用实时技术位计算止损止盈
            bot_sl = (price + sl_pts) if direction == "做空" else (price - sl_pts)
            bot_tp = (price - TARGET_POINTS) if direction == "做空" else (price + TARGET_POINTS)
            bot_targets = []

            # 关键位止损优先（强度>=2时）
            if use_level_sl and la_strength >= 2:
                bot_sl = la_sl_hint
                sl_pts = round(abs(price - bot_sl))
                log("关键位止损: ${:,.0f}（{}）".format(bot_sl, la.get("description", "")))

            # 止盈目标：顺势用动态参数，逆势用Claude决定的点数
            try:
                c1h = get_klines("1H", 50)
                c4h = get_klines("4H", 20)
                ob = None  # get_orderbook不在此版本
                levels = calc_key_levels(c1h, c4h, price, ob) if c1h and c4h else []

                t1, t2, t3 = dp["trend_target_1"], dp["trend_target_2"], dp["trend_target_3"]

                if is_counter_trend:
                    tp1 = counter_trend_target
                    if direction == "做空":
                        if levels:
                            resistance = [l for l in levels if l["price"] > price + 200]
                            if resistance:
                                bot_sl = sorted(resistance, key=lambda x: x["price"])[0]["price"] + 50
                                sl_pts = round(bot_sl - price)
                        bot_targets = [{"price": price - tp1, "label": "逆势目标({}点)".format(tp1),
                                        "ratio": 1.0, "sources": ["逆势短线"]}]
                        bot_tp = price - tp1
                    else:
                        if levels:
                            support = [l for l in levels if l["price"] < price - 200]
                            if support:
                                bot_sl = sorted(support, key=lambda x: x["price"], reverse=True)[0]["price"] - 50
                                sl_pts = round(price - bot_sl)
                        bot_targets = [{"price": price + tp1, "label": "逆势目标({}点)".format(tp1),
                                        "ratio": 1.0, "sources": ["逆势短线"]}]
                        bot_tp = price + tp1
                    log("逆势短线模式: 目标{}点 止损{}点".format(tp1, sl_pts))

                elif direction == "做空":
                    resistance = [l for l in levels if l["price"] > price + 500]
                    if resistance:
                        bot_sl = sorted(resistance, key=lambda x: x["price"])[0]["price"] + 50
                        sl_pts = round(bot_sl - price)
                    bot_targets = [
                        {"price": price - t1, "label": "第一目标({}点)".format(t1),  "ratio": 0.3, "sources": ["动态参数"]},
                        {"price": price - t2, "label": "第二目标({}点)".format(t2), "ratio": 0.3, "sources": ["动态参数"]},
                        {"price": price - t3, "label": "最终目标({}点)".format(t3), "ratio": 1.0, "sources": ["动态参数"]},
                    ]
                    bot_tp = price - t3
                else:
                    support = [l for l in levels if l["price"] < price - 500]
                    if support:
                        bot_sl = sorted(support, key=lambda x: x["price"], reverse=True)[0]["price"] - 50
                        sl_pts = round(price - bot_sl)
                    bot_targets = [
                        {"price": price + t1, "label": "第一目标({}点)".format(t1),  "ratio": 0.3, "sources": ["动态参数"]},
                        {"price": price + t2, "label": "第二目标({}点)".format(t2), "ratio": 0.3, "sources": ["动态参数"]},
                        {"price": price + t3, "label": "最终目标({}点)".format(t3), "ratio": 1.0, "sources": ["动态参数"]},
                    ]
                    bot_tp = price + t3

            except Exception as e:
                log("龙虾开仓技术位计算失败: {}".format(e))
                t1, t2, t3 = dp["trend_target_1"], dp["trend_target_2"], dp["trend_target_3"]
                if direction == "做空":
                    bot_targets = [
                        {"price": price - t1, "label": "第一目标({}点)".format(t1),  "ratio": 0.3, "sources": ["动态参数"]},
                        {"price": price - t2, "label": "第二目标({}点)".format(t2), "ratio": 0.3, "sources": ["动态参数"]},
                        {"price": price - t3, "label": "最终目标({}点)".format(t3), "ratio": 1.0, "sources": ["动态参数"]},
                    ]
                    bot_tp = price - t3
                else:
                    bot_targets = [
                        {"price": price + t1, "label": "第一目标({}点)".format(t1),  "ratio": 0.3, "sources": ["动态参数"]},
                        {"price": price + t2, "label": "第二目标({}点)".format(t2), "ratio": 0.3, "sources": ["动态参数"]},
                        {"price": price + t3, "label": "最终目标({}点)".format(t3), "ratio": 1.0, "sources": ["动态参数"]},
                    ]
                    bot_tp = price + t3

            # ── 交易日记：开仓前完整思考 ──────────────────────
            diary_prompt = (
                "你是BTC合约交易龙虾，即将开仓，先完整思考这笔交易。\n\n"
                "【市场状态】{} — {}\n"
                "【信号】{} {} @ ${:,.0f} | 趋势: {}\n"
                "【技术面】RSI日线={} RSI1H={} MACD1H={} 成交量比={}\n"
                "【情绪面】资金费率={} 订单簿={}\n"
                "【风险】止损{}点 仓位{}BTC 最大亏损${:.0f}\n"
                "【目标】资金${:.0f} 还差翻倍${:.0f}\n"
                "【近期状态】最近3次亏损:{}次\n\n"
                "请用4句话完成开仓前思考：\n"
                "第1句：这笔交易最强的入场理由是什么\n"
                "第2句：最大的风险和不确定性是什么\n"
                "第3句：预期行情会怎么走，目标价位\n"
                "第4句：什么情况下我会提前止盈或止损"
            ).format(
                mkt_state, mkt_desc,
                direction, signal.get("strength", "中"), price, trend_desc,
                market_data.get("日线", {}).get("RSI", "N/A"),
                market_data.get("1小时", {}).get("RSI", "N/A"),
                market_data.get("1小时", {}).get("Histogram", "N/A"),
                market_data.get("1小时", {}).get("成交量比率", "N/A"),
                market_data.get("资金费率", "N/A"),
                market_data.get("订单簿", {}).get("bias", "N/A"),
                sl_pts, position_size, position_size * sl_pts,
                current_capital, remaining,
                recent_losses
            )
            trade_diary = claude_request(diary_prompt, max_tokens=200)
            log("开仓日记: {}".format((trade_diary or "")[:100]))

            memory["bot_trade"] = {
                "direction": direction, "entry_price": price,
                "size": position_size, "stop_loss": bot_sl, "target": bot_tp,
                "targets": bot_targets, "stop_loss_points": sl_pts,
                "time": datetime.now().strftime("%m-%d %H:%M"),
                "open_reason": signal.get("push_reason", "综合技术面判断"),
                "signal_strength": signal.get("strength", "中"),
                "open_analysis": analysis[:200] if analysis else "",
                "market_state": mkt_state,
                "trend_desc": trend_desc,
                "is_counter_trend": is_counter_trend,
                "trade_diary": trade_diary or "",
                "entry_rsi_1h": market_data.get("1小时", {}).get("RSI"),
                "entry_rsi_4h": market_data.get("4小时", {}).get("RSI"),
                "entry_macd_1h": market_data.get("1小时", {}).get("Histogram"),
                "entry_vol_ratio": market_data.get("1小时", {}).get("成交量比率"),
                "entry_funding": market_data.get("资金费率"),
                "entry_ob_bias": market_data.get("订单簿", {}).get("bias"),
            }
            save_memory(memory)

            pnl_risk = position_size * sl_pts
            msg = "🦞 <b>龙虾开仓</b>\n"
            msg += "─────────────────────\n"
            msg += "方向: <b>{}</b> @ ${:,.0f} | {}\n".format(direction, price, mkt_state)
            msg += "仓位: {} BTC | 风险: ${:.0f} | {}\n".format(position_size, pnl_risk, trend_desc)
            msg += "止损: ${:,.0f}（{}点）\n".format(bot_sl, sl_pts)
            if bot_targets:
                for tg in bot_targets[:2]:
                    msg += "目标: ${:,.0f}（+{:.0f}点）\n".format(tg["price"], abs(tg["price"] - price))
            if trade_diary:
                msg += "\n<b>开仓思考：</b>\n{}\n".format(trade_diary.strip()[:300])
            msg += "\n资金: ${:,.0f} | 还差翻倍: ${:,.0f}".format(current_capital, max(remaining, 0))
            send_telegram(msg)
            log("龙虾自动开仓: {} {} BTC @ ${:,.0f} | {}".format(direction, position_size, price, mkt_state))

    # 检查龙虾自己的持仓
    bot_trade = memory.get("bot_trade")
    if bot_trade and price:
        bt_result, bt_pnl = None, 0
        sl_pts = bot_trade.get("stop_loss_points", STOP_LOSS_DEFAULT)
        direction = bot_trade["direction"]
        ep = bot_trade["entry_price"]
        size = bot_trade["size"]
        targets = bot_trade.get("targets", [])

        # 计算当前浮盈
        if direction == "做空":
            pnl_now = round(ep - price)
        else:
            pnl_now = round(price - ep)

        # ── 移动止损 ──────────────────────────────────────
        # 浮盈 >= 500点 → 保本
        # 浮盈 >= 800点 → 锁500点
        # 浮盈 >= 1200点 → 锁800点
        # 浮盈 >= 1500点 → 锁1200点
        # 浮盈 >= 1800点 → 锁1500点
        current_sl = bot_trade["stop_loss"]
        new_sl = current_sl
        if pnl_now >= 1800:
            lock_pts = 1500
            new_sl = (ep - lock_pts) if direction == "做空" else (ep + lock_pts)
        elif pnl_now >= 1500:
            lock_pts = 1200
            new_sl = (ep - lock_pts) if direction == "做空" else (ep + lock_pts)
        elif pnl_now >= 1200:
            lock_pts = 800
            new_sl = (ep - lock_pts) if direction == "做空" else (ep + lock_pts)
        elif pnl_now >= 800:
            lock_pts = 500
            new_sl = (ep - lock_pts) if direction == "做空" else (ep + lock_pts)
        elif pnl_now >= 500:
            lock_pts = 0
            new_sl = ep  # 移到保本

        # 止损只能往盈利方向移，不能往回退
        if direction == "做多" and new_sl > current_sl:
            memory["bot_trade"]["stop_loss"] = new_sl
            save_memory(memory)
            bot_trade["stop_loss"] = new_sl
            log("移动止损: 做多 止损上移至 ${:,.0f}（浮盈{}点）".format(new_sl, pnl_now))
        elif direction == "做空" and new_sl < current_sl:
            memory["bot_trade"]["stop_loss"] = new_sl
            save_memory(memory)
            bot_trade["stop_loss"] = new_sl
            log("移动止损: 做空 止损下移至 ${:,.0f}（浮盈{}点）".format(new_sl, pnl_now))
        # ──────────────────────────────────────────────────

        # ── 顺势加仓：浮盈500点且没有加仓过 ──────────────
        if (pnl_now >= 500 and
            not bot_trade.get("added_position") and
            not memory.get("bot_trade2")):
            add_size = round(size * 0.5, 3)
            add_sl = (ep + 200) if direction == "做空" else (ep - 200)
            add_tp = (price - 1500) if direction == "做空" else (price + 1500)
            memory["bot_trade"]["added_position"] = True
            memory["bot_trade2"] = {
                "direction": direction,
                "entry_price": price,
                "size": add_size,
                "stop_loss": add_sl,
                "target": add_tp,
                "targets": [{"price": add_tp, "ratio": 1.0, "label": "加仓止盈+1500点"}],
                "stop_loss_points": 200,
                "time": datetime.now().strftime("%m-%d %H:%M"),
                "is_add": True
            }
            save_memory(memory)
            msg = "➕ <b>龙虾顺势加仓</b>\n"
            msg += "方向: {} | 原仓浮盈{:+.0f}点\n".format(direction, pnl_now)
            msg += "加仓: {} BTC @ ${:,.0f}\n".format(add_size, price)
            msg += "止损: ${:,.0f} | 目标: ${:,.0f}".format(add_sl, add_tp)
            send_telegram(msg)
            log("顺势加仓: {} {}BTC @ ${:,.0f}".format(direction, add_size, price))

        # 止损检查
        if direction == "做空" and price >= bot_trade["stop_loss"]:
            bt_result, bt_pnl = "止损", -sl_pts
        elif direction == "做多" and price <= bot_trade["stop_loss"]:
            bt_result, bt_pnl = "止损", -sl_pts

        # 到达目标位时的止盈逻辑
        if not bt_result and targets:
            hit_target = None
            for tg in targets:
                if direction == "做空" and price <= tg["price"]:
                    hit_target = tg
                    break
                elif direction == "做多" and price >= tg["price"]:
                    hit_target = tg
                    break

            if hit_target and can_alert("bot_tp_think_{}".format(int(hit_target["price"])), cooldown=300):
                remaining_targets = targets[targets.index(hit_target)+1:]
                has_more_targets = len(remaining_targets) > 0
                default_ratio = hit_target.get("ratio", 1.0)  # 每个目标预设减仓比例

                # 让Claude决定：情况不对就全部止盈，情况好就按预设减仓
                think_prompt = (
                    "BTC合约交易，现在需要决定止盈策略。\n"
                    "方向:{} 入场:${:,.0f} 当前:${:,.0f} 浮盈:{:+.0f}点 仓位:{}BTC\n"
                    "刚到达 {}: ${:,.0f}\n"
                    "{}\n"
                    "RSI(1H)={} 订单簿={} 资金费率={}\n\n"
                    "预设计划：减仓{}成（{}%）继续持有剩余。\n"
                    "如果市场情况不对（RSI极端/订单簿压力大/趋势反转），就全部止盈。\n"
                    "必须写：【决定】全部止盈 或 【决定】减仓{}成，一句话理由。"
                ).format(
                    direction, ep, price, pnl_now, size,
                    hit_target["label"], hit_target["price"],
                    "还有更多目标: {}".format(", ".join(["${}".format(int(t["price"])) for t in remaining_targets])) if has_more_targets else "这是最后目标，建议全部止盈",
                    market_data.get("1小时", {}).get("RSI", "N/A"),
                    market_data.get("订单簿", {}).get("bias", "N/A"),
                    market_data.get("资金费率", "N/A"),
                    int(default_ratio * 10), int(default_ratio * 100),
                    int(default_ratio * 10),
                )

                think_result = claude_request(think_prompt, max_tokens=100)
                if not think_result:
                    # Claude失败就按预设比例执行
                    think_result = "【决定】减仓{}成".format(int(default_ratio * 10)) if has_more_targets else "【决定】全部止盈"

                log("龙虾止盈思考: {}".format(think_result[:100]))

                # 解析决定
                ratio_match = re.search(r'减仓(\d+)成', think_result)
                reduce_ratio = int(ratio_match.group(1)) / 10.0 if ratio_match else default_ratio

                if "【决定】全部止盈" in think_result or not has_more_targets:
                    # 全部止盈
                    bt_pnl = pnl_now
                    bt_result = "止盈({})".format(hit_target["label"])
                    msg = "✅ <b>龙虾全部止盈</b>\n"
                    msg += "到达 {}: ${:,.0f}\n".format(hit_target["label"], hit_target["price"])
                    msg += "盈利: {:+.0f}点 / {:+.0f} USDT\n".format(pnl_now, pnl_now * size)
                    msg += "理由: {}\n".format(think_result.replace("【决定】全部止盈", "").strip())
                    send_telegram(msg)

                elif "【决定】减仓" in think_result and reduce_ratio < 1.0:
                    # 分批止盈
                    reduce_size = round(size * reduce_ratio, 4)
                    remain_size = round(size - reduce_size, 4)
                    partial_pnl = round(pnl_now * reduce_ratio)
                    partial_usd = round(partial_pnl * reduce_size, 2)

                    memory["bot_trade"]["size"] = remain_size
                    remaining_after = [t for t in targets if t != hit_target]
                    memory["bot_trade"]["targets"] = remaining_after
                    bs = memory["bot_stats"]
                    bs["total_pnl_points"] = bs.get("total_pnl_points", 0) + partial_pnl
                    bs["total_pnl_usdt"] = bs.get("total_pnl_usdt", 0) + partial_usd
                    save_memory(memory)

                    msg = "💰 <b>龙虾分批止盈</b>\n"
                    msg += "到达 {}: ${:,.0f}\n".format(hit_target["label"], hit_target["price"])
                    msg += "减仓 {}成（{} BTC）锁定 {:+.0f}点 / {:+.0f} USDT\n".format(
                        int(reduce_ratio*10), reduce_size, partial_pnl, partial_usd)
                    msg += "剩余 {} BTC 继续持有\n".format(remain_size)
                    if remaining_after:
                        msg += "下一目标: ${:,.0f}\n".format(remaining_after[0]["price"])
                    msg += "理由: {}\n".format(think_result.replace("【决定】减仓{}成".format(int(reduce_ratio*10)), "").strip())
                    send_telegram(msg)
                    log("龙虾分批止盈: 减{}成 剩余{}BTC".format(int(reduce_ratio*10), remain_size))

        # 止损/止盈预警
        dist_sl = abs(price - bot_trade["stop_loss"])
        if dist_sl <= 100 and can_alert("bot_sl_warn", cooldown=120):
            msg = "<b>龙虾止损预警</b>\n"
            msg += "{}持仓接近止损位\n".format(direction)
            msg += "当前: ${:,.0f} | 止损: ${:,.0f}（距{}点）".format(
                price, bot_trade["stop_loss"], int(dist_sl))
            send_telegram(msg)

        if bt_result:
            pnl_usd = abs(bt_pnl) * size
            bs = memory["bot_stats"]
            bs["total"] += 1
            bs["wins" if "止盈" in bt_result else "losses"] += 1
            bs["total_pnl_points"] += bt_pnl
            bs["total_pnl_usdt"] = bs.get("total_pnl_usdt", 0) + (pnl_usd if "止盈" in bt_result else -pnl_usd)

            # ── 完整复盘：预期 vs 实际 + 经验总结 ──────────────
            original_diary = bot_trade.get("trade_diary", "无开仓日记")
            entry_rsi  = bot_trade.get("entry_rsi_1h", "N/A")
            entry_macd = bot_trade.get("entry_macd_1h", "N/A")
            entry_vol  = bot_trade.get("entry_vol_ratio", "N/A")
            trade_mkt  = bot_trade.get("market_state", "未知")
            trade_trend = bot_trade.get("trend_desc", "未知")

            reflection_prompt = (
                "你是BTC合约交易龙虾，刚完成一笔交易，现在做深度复盘。\n\n"
                "【交易记录】\n"
                "方向:{} | 入场:${:,.0f} | 平仓:${:,.0f} | 结果:{} {:+.0f}点\n"
                "入场时市场:{} | 趋势:{}\n"
                "入场时RSI1H:{} MACD:{} 成交量比:{}\n\n"
                "【开仓前你的思考】\n{}\n\n"
                "【当前市场状态】\n"
                "RSI1H={} 订单簿={}\n\n"
                "请用4句话深度复盘：\n"
                "第1句：预期和实际的差距在哪里（对比开仓日记）\n"
                "第2句：入场时机和参数设置对不对\n"
                "第3句：下次遇到同样的市场状态会怎么做\n"
                "第4句：给这笔交易打分X/10，并说一个具体改进点"
            ).format(
                bot_trade["direction"], bot_trade["entry_price"], price,
                bt_result, bt_pnl,
                trade_mkt, trade_trend,
                entry_rsi, entry_macd, entry_vol,
                original_diary[:200] if original_diary else "无",
                market_data.get("1小时", {}).get("RSI", "N/A"),
                market_data.get("订单簿", {}).get("bias", "N/A"),
            )
            reflection = claude_request(reflection_prompt, max_tokens=250)

            # ── 止损后反手判断 ──────────────────────────────────
            reverse_signal = None
            if "止损" in bt_result:
                reverse_prompt = (
                    "你刚{} @ ${:,.0f} 止损了，亏了{:.0f}点。\n"
                    "当前价: ${:,.0f} | RSI1H={} | 市场状态:{}\n"
                    "止损说明之前判断错误。现在需要判断：\n"
                    "1. 趋势是否已经反转？\n"
                    "2. 有没有反手{}的机会？\n"
                    "必须回答：【反手】做多/做空/不操作，一句话理由"
                ).format(
                    direction, ep, abs(bt_pnl), price,
                    market_data.get("1小时", {}).get("RSI", "N/A"),
                    mkt_state,
                    "做多" if direction == "做空" else "做空"
                )
                reverse_result = claude_request(reverse_prompt, max_tokens=80)
                if reverse_result:
                    log("反手判断: {}".format(reverse_result[:80]))
                    if "【反手】做多" in reverse_result or "【反手】做空" in reverse_result:
                        if "不操作" not in reverse_result:
                            reverse_dir = "做多" if "【反手】做多" in reverse_result else "做空"
                            reverse_signal = {"direction": reverse_dir, "reason": reverse_result}

            # 写入龙虾记忆
            now = datetime.now().strftime("%m-%d %H:%M")
            if "bot_reflections" not in memory:
                memory["bot_reflections"] = []
            memory["bot_reflections"].append({
                "time": now,
                "trade": "{} {:+.0f}点 ({})".format(bot_trade["direction"], bt_pnl, trade_mkt),
                "reflection": reflection or "无复盘",
                "entry_data": {
                    "rsi_1h": entry_rsi, "macd": entry_macd,
                    "vol": entry_vol, "market": trade_mkt
                }
            })
            memory["bot_reflections"] = memory["bot_reflections"][-20:]
            memory["bot_trade"] = None
            save_memory(memory)

            emoji = "✅" if "止盈" in bt_result else "❌"
            msg = "{} <b>龙虾{}</b>\n".format(emoji, bt_result)
            msg += "方向: {} | {:+.0f}点 / {:+.0f} USDT\n".format(direction, bt_pnl, pnl_usd)
            msg += "市场: {} | {}\n".format(trade_mkt, trade_trend)
            msg += "战绩: {}胜{}负 | 累计{:+.0f}点\n\n".format(
                bs["wins"], bs["losses"], bs["total_pnl_points"])
            if reflection:
                msg += "<b>深度复盘：</b>\n{}\n".format(reflection.strip()[:400])

            # 如果决定反手，附上反手信号
            if reverse_signal:
                msg += "\n🔄 <b>龙虾决定反手</b>: {}\n".format(reverse_signal["direction"])
                msg += "理由: {}\n".format(reverse_signal.get("reason", "")[:80])

            send_telegram(msg)
            log("龙虾持仓{}: {:+.0f}点 | {}".format(bt_result, bt_pnl, trade_mkt))

            # 反手开仓（让下一个周期的信号决定，这里只记录意图）
            if reverse_signal:
                memory["pending_reverse"] = {
                    "direction": reverse_signal["direction"],
                    "reason": reverse_signal.get("reason", ""),
                    "time": now,
                    "expire_after": 2  # 2个周期内有效
                }
                save_memory(memory)

    if result:
        send_telegram(format_push_message(analysis, price, signal, result, pnl))
    elif signal["should_push"]:
        if memory.get("real_trade"):
            log("持仓中，跳过新信号推送，专注监控持仓")
        else:
            last_price = memory.get("last_push_price", price)
            last_direction = memory.get("last_push_direction", "")
            last_price = memory.get("last_push_price", price)
            price_move = abs(price - last_price)
            is_opposite = signal["direction"] != last_direction and last_direction != ""
            big_reversal = price_move >= 1000  # 反向必须1000点以上

            # 龙虾有持仓时，推送方向必须和持仓一致
            bot_trade_now = memory.get("bot_trade")
            if bot_trade_now:
                bot_dir = bot_trade_now.get("direction", "")
                if bot_dir and signal["direction"] != bot_dir:
                    log("龙虾持仓{}，推送方向{}不一致，跳过".format(bot_dir, signal["direction"]))
                    return

            # 反向信号必须价格变动1000点以上
            if is_opposite and not big_reversal:
                log("反向信号但价格仅波动{:.0f}点不足1000点，跳过".format(price_move))
            # 同方向：45分钟冷却（原来30分钟太短）
            elif not can_alert("signal_push", cooldown=2700):
                log("推送冷却中，跳过")
            # 市场震荡时（多空得分差距小），不推送
            elif abs(score_long - score_short) < 2 and not (trend_bias):
                log("市场方向不明（多{}空{}），不推送噪音信号".format(score_long, score_short))
            else:
                send_telegram(format_push_message(analysis, price, signal))
                last_alert_time["signal_push"] = time.time()
                memory["last_push_price"] = price
                memory["last_push_direction"] = signal["direction"]
                save_memory(memory)
                log("已推送到Telegram")
    else:
        log("龙虾认为无需打扰，继续监测...")

def param_review(memory, market_data):
    """每完成5笔交易，龙虾复盘交易结果+入场数据，自主调整动态参数"""
    signals = memory.get("signals", [])
    dp = memory.get("dynamic_params", {})
    last_count = dp.get("last_review_count", 0)
    total = memory.get("bot_stats", {}).get("total", 0)

    # 每5笔触发一次
    if total - last_count < 5:
        return

    recent = signals[-10:]
    if len(recent) < 5:
        return

    log("触发参数复盘（已完成{}笔交易）".format(total))

    # 整理近期交易详情，包括入场时的技术数据
    trade_details = []
    for s in recent:
        detail = "{} {} 入场${:.0f} {} {:+.0f}点".format(
            s.get("time", ""), s.get("direction", ""),
            s.get("price_at_signal", 0), s.get("result", ""),
            s.get("pnl_points", 0))
        trade_details.append(detail)

    wins = sum(1 for s in recent if s.get("pnl_points", 0) > 0)
    losses = len(recent) - wins
    avg_win = sum(s.get("pnl_points", 0) for s in recent if s.get("pnl_points", 0) > 0) / max(wins, 1)
    avg_loss = abs(sum(s.get("pnl_points", 0) for s in recent if s.get("pnl_points", 0) < 0)) / max(losses, 1)

    # 当前资金和目标
    bs = memory.get("bot_stats", {})
    current_capital = 3300 + bs.get("total_pnl_usdt", 0)
    goal = memory.get("goal", {})
    round_target = goal.get("round_target", 6600)
    remaining = max(round_target - current_capital, 0)

    # 用户战绩对比
    user_signals = signals[-10:]
    u_wins = sum(1 for s in user_signals if s.get("pnl_points", 0) > 0)
    u_pnl = sum(s.get("pnl_points", 0) for s in user_signals)

    prompt = (
        "你是BTC合约交易龙虾，现在做参数复盘优化。终极目标：从${:.0f}翻倍到${:.0f}，还差${:.0f}。\n\n"
        "【最近{}笔交易结果】\n{}\n"
        "小计: {}胜{}负 | 平均盈利{:.0f}点 | 平均亏损{:.0f}点\n\n"
        "【用户最近10笔对比】{}胜{}负 累计{:+.0f}点（用户胜率更高，值得参考）\n\n"
        "【当前动态参数】\n"
        "止损范围: {}-{}点\n"
        "顺势目标: {}点 / {}点 / {}点\n"
        "逆势目标: {}点\n"
        "仓位: 强信号{}BTC 普通{}BTC 逆势{}BTC\n\n"
        "请分析：哪些参数导致了亏损？哪些需要调整才能更快翻倍？\n"
        "每个参数调整幅度不超过20%，给出新参数值。\n"
        "必须按以下格式输出（每行一个）：\n"
        "止损最小:XXX\n止损最大:XXX\n顺势目标1:XXX\n顺势目标2:XXX\n顺势目标3:XXX\n"
        "逆势目标:XXX\n强信号仓位:0.XX\n普通仓位:0.XX\n逆势仓位:0.XX\n理由:一句话"
    ).format(
        current_capital, round_target, remaining,
        len(recent), "\n".join(trade_details),
        wins, losses, avg_win, avg_loss,
        u_wins, len(user_signals) - u_wins, u_pnl,
        dp.get("stop_loss_min", 500), dp.get("stop_loss_max", 800),
        dp.get("trend_target_1", 500), dp.get("trend_target_2", 1000), dp.get("trend_target_3", 2000),
        dp.get("counter_trend_target", 300),
        dp.get("position_strong", 0.2), dp.get("position_normal", 0.1), dp.get("position_counter", 0.1)
    )

    result = claude_request(prompt, max_tokens=250)
    if not result:
        log("参数复盘失败，跳过")
        return

    log("参数复盘结果: {}".format(result[:200]))

    # 解析新参数，带边界保护
    def parse_param(text, key, current, min_val, max_val):
        m = re.search(r'{}[：:]\s*(\d+\.?\d*)'.format(key), text)
        if m:
            new_val = float(m.group(1))
            # 每次调整不超过20%
            max_change = current * 0.2
            new_val = max(current - max_change, min(current + max_change, new_val))
            return max(min_val, min(max_val, round(new_val, 2)))
        return current

    old_params = dict(dp)
    dp["stop_loss_min"]        = parse_param(result, "止损最小", dp.get("stop_loss_min", 500), 300, 700)
    dp["stop_loss_max"]        = parse_param(result, "止损最大", dp.get("stop_loss_max", 800), 500, 1200)
    dp["trend_target_1"]       = parse_param(result, "顺势目标1", dp.get("trend_target_1", 500), 300, 800)
    dp["trend_target_2"]       = parse_param(result, "顺势目标2", dp.get("trend_target_2", 1000), 600, 1500)
    dp["trend_target_3"]       = parse_param(result, "顺势目标3", dp.get("trend_target_3", 2000), 1000, 3000)
    dp["counter_trend_target"] = parse_param(result, "逆势目标", dp.get("counter_trend_target", 300), 150, 600)
    dp["position_strong"]      = parse_param(result, "强信号仓位", dp.get("position_strong", 0.2), 0.05, 0.3)
    dp["position_normal"]      = parse_param(result, "普通仓位", dp.get("position_normal", 0.1), 0.05, 0.2)
    dp["position_counter"]     = parse_param(result, "逆势仓位", dp.get("position_counter", 0.1), 0.05, 0.2)
    dp["last_review_count"]    = total

    # 记录参数变化历史
    changes = []
    for k in ["stop_loss_min", "stop_loss_max", "trend_target_1", "trend_target_2",
              "trend_target_3", "counter_trend_target", "position_strong", "position_normal", "position_counter"]:
        if abs(dp[k] - old_params.get(k, dp[k])) > 0.001:
            changes.append("{}: {} → {}".format(k, old_params.get(k), dp[k]))

    if not isinstance(dp.get("param_history"), list):
        dp["param_history"] = []
    dp["param_history"].append({
        "time": datetime.now().strftime("%m-%d %H:%M"),
        "changes": changes,
        "reason": result[-100:] if result else ""
    })
    dp["param_history"] = dp["param_history"][-10:]

    memory["dynamic_params"] = dp
    save_memory(memory)

    if changes:
        msg = "🧠 <b>龙虾自我进化</b>\n"
        msg += "完成{}笔交易后调整参数：\n".format(total)
        for c in changes:
            msg += "· {}\n".format(c)
        # 提取理由
        reason_match = re.search(r'理由[：:](.*)', result)
        if reason_match:
            msg += "\n理由: {}\n".format(reason_match.group(1).strip()[:80])
        send_telegram(msg)
        log("参数已更新: {}".format(", ".join(changes)))
    else:
        log("参数复盘完成，无需调整")


def daily_review():
    """每天凌晨2点，对比用户和龙虾的交易记录，生成学习总结"""
    memory = load_memory()
    signals = memory.get("signals", [])
    bot_refs = memory.get("bot_reflections", [])

    if len(signals) < 3:
        log("交易记录不足，跳过每日复盘")
        return

    # 整理用户最近10笔
    recent = signals[-10:]
    user_lines = []
    for s in recent:
        user_lines.append("{} {} 入场${:.0f} {} {:+.0f}点".format(
            s.get("time", ""), s.get("direction", ""),
            s.get("price_at_signal", 0), s.get("result", ""),
            s.get("pnl_points", 0)))
    user_wins = sum(1 for s in recent if s.get("pnl_points", 0) > 0)
    user_pnl = sum(s.get("pnl_points", 0) for s in recent)

    # 整理龙虾最近复盘
    bot_lines = []
    for r in bot_refs[-5:]:
        bot_lines.append("{} {} {}".format(
            r.get("time", ""), r.get("trade", ""),
            r.get("reflection", "")[:50]))

    prompt = (
        "你是BTC合约交易员龙虾，现在做每日复盘对比学习。\n\n"
        "【用户最近{}笔交易】（用户是你的参考对象，他的胜率比你高）\n"
        "{}\n"
        "用户小计: {}胜{}负 {:+.0f}点\n\n"
        "【你自己最近的复盘】\n"
        "{}\n\n"
        "请用3句话总结：\n"
        "第1句：用户做得比你好在哪里（从交易结果分析）\n"
        "第2句：你下次遇到类似情况应该怎么调整\n"
        "第3句：给自己和用户各打一个分（X/10）"
    ).format(
        len(recent),
        "\n".join(user_lines),
        user_wins, len(recent) - user_wins, user_pnl,
        "\n".join(bot_lines) if bot_lines else "暂无复盘记录"
    )

    result = claude_request(prompt, max_tokens=200)
    if result:
        now = datetime.now().strftime("%m-%d %H:%M")
        if "bot_reflections" not in memory:
            memory["bot_reflections"] = []
        memory["bot_reflections"].append({
            "time": now,
            "trade": "每日对比复盘",
            "reflection": result
        })
        memory["bot_reflections"] = memory["bot_reflections"][-20:]
        save_memory(memory)
        log("每日对比复盘完成")
    else:
        log("每日对比复盘失败")


def main():
    os.environ["https_proxy"] = "http://127.0.0.1:7897"
    os.environ["http_proxy"] = "http://127.0.0.1:7897"

    log("BTC 龙虾交易大脑 v6.9.5 启动")
    log("本金: ${:,} | 仓位: {}-{} BTC".format(CAPITAL, POSITION_DEFAULT, POSITION_MAX))
    log("动态止损: {}-{}点 | 目标: {}点".format(STOP_LOSS_MIN, STOP_LOSS_MAX, TARGET_POINTS))
    log("监测周期: 白天{}分钟/凌晨{}分钟".format(CHECK_INTERVAL_DAY//60, CHECK_INTERVAL_NIGHT//60))
    log("TG指令: 自然语言发送即可，龙虾自动理解")
    log("="*50)

    # 启动预警线程
    threading.Thread(target=alert_thread, daemon=True).start()
    # 启动TG监听线程
    threading.Thread(target=poll_telegram_commands, daemon=True).start()
    log("TG指令监听已启动")

    run_analysis()

    last_review_day = -1
    while True:
        interval = get_check_interval()
        log("等待{}分钟...（{}）".format(
            interval//60, "凌晨降频" if interval == CHECK_INTERVAL_NIGHT else "白天正常"))
        time.sleep(interval)

        # 每天凌晨2点触发对比复盘
        now = datetime.now()
        if now.hour == 2 and now.day != last_review_day:
            log("触发每日对比复盘...")
            threading.Thread(target=daily_review, daemon=True).start()
            last_review_day = now.day

        run_analysis()

if __name__ == "__main__":
    main()
