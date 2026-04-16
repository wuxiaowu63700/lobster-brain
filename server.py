#!/usr/bin/env python3
"""
龙虾交易大脑 Web UI — Flask 后端
运行: python3 server.py
然后打开浏览器: http://localhost:5000
"""

import os
os.environ['https_proxy'] = 'http://127.0.0.1:7897'
os.environ['http_proxy'] = 'http://127.0.0.1:7897'
os.environ['ALL_PROXY'] = 'http://127.0.0.1:7897'
import json
import os
import time
import requests
import urllib3
from flask import Flask, jsonify, request, send_from_directory
from datetime import datetime

urllib3.disable_warnings()

app = Flask(__name__, static_folder='.')

MEMORY_FILE = os.path.expanduser("~/.btc_monitor/memory.json")
LOG_FILE = os.path.expanduser("~/.btc_monitor/brain.log")

def load_memory():
    if not os.path.exists(MEMORY_FILE):
        return {}
    with open(MEMORY_FILE) as f:
        return json.load(f)

def save_memory(memory):
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)

def get_price(symbol="BTC-USDT"):
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/market/ticker",
            params={"instId": symbol}, timeout=5, verify=False
        )
        return float(r.json()["data"][0]["last"])
    except:
        return None

def get_eth_price():
    return get_price("ETH-USDT")

# ── API ─────────────────────────────────────────────

@app.route("/api/market")
def api_market():
    btc = get_price("BTC-USDT")
    eth = get_eth_price()
    # 24h change
    try:
        r = requests.get("https://www.okx.com/api/v5/market/ticker",
                         params={"instId": "BTC-USDT"}, timeout=5, verify=False)
        d = r.json()["data"][0]
        open24 = float(d["open24h"])
        last   = float(d["last"])
        change_pct = (last - open24) / open24 * 100
        vol24  = float(d["volCcy24h"])
    except:
        change_pct = 0
        vol24 = 0
    # funding
    try:
        r2 = requests.get(
            "https://www.okx.com/api/v5/public/funding-rate",
            params={"instId": "BTC-USDT-SWAP"}, timeout=5, verify=False
        )
        funding = float(r2.json()["data"][0]["fundingRate"]) * 100
    except:
        funding = None
    # fear & greed
    try:
        r3 = requests.get("https://api.alternative.me/fng/?limit=1", timeout=5)
        fg = r3.json()["data"][0]
        fear_value = int(fg["value"])
        fear_label = fg["value_classification"]
    except:
        fear_value = None
        fear_label = "N/A"
    return jsonify({
        "btc": btc, "eth": eth,
        "change_pct": round(change_pct, 2),
        "vol24": round(vol24, 0),
        "funding": round(funding, 4) if funding is not None else None,
        "fear_value": fear_value, "fear_label": fear_label,
        "ts": datetime.now().strftime("%H:%M:%S")
    })

@app.route("/api/state")
def api_state():
    mem = load_memory()
    btc = get_price()
    eth = get_eth_price()

    # 计算浮盈
    def calc_pnl(trade, price):
        if not trade or not price:
            return 0
        ep = trade.get("entry_price", 0)
        size = trade.get("size", 0.1)
        if trade.get("direction") == "做多":
            return round((price - ep) * size, 2)
        else:
            return round((ep - price) * size, 2)

    bot_trade = mem.get("bot_trade")
    real_trades = mem.get("real_trades", [])
    if not real_trades and mem.get("real_trade"):
        real_trades = [mem["real_trade"]]

    # 丰富 real_trades 的浮盈
    for t in real_trades:
        p = eth if t.get("symbol") == "ETH" else btc
        t["pnl_now"] = calc_pnl(t, p)
        t["current_price"] = p

    bot_pnl_now = calc_pnl(bot_trade, btc) if bot_trade else 0
    if bot_trade:
        bot_trade["pnl_now"] = bot_pnl_now

    stats = mem.get("stats", {})
    bot_stats = mem.get("bot_stats", {})
    INIT = 3300.0
    user_total = INIT + stats.get("total_pnl_usdt", stats.get("total_pnl_points", 0) * 0.1)
    bot_total  = INIT + bot_stats.get("total_pnl_usdt", bot_stats.get("total_pnl_points", 0) * 0.1)

    # 账户曲线：从 signals 里提取累计盈亏
    signals = mem.get("signals", [])
    curve_user = []
    curve_bot  = []
    cumulative = 0
    for s in signals[-30:]:
        cumulative += s.get("pnl_points", 0) * 0.1
        curve_user.append(round(INIT + cumulative, 2))
    bot_reflections = mem.get("bot_reflections", [])
    bot_cum = 0
    for i, s in enumerate(signals[-30:]):
        bot_cum += s.get("pnl_points", 0) * 0.1 * 0.5
        curve_bot.append(round(INIT + bot_cum, 2))

    goal = mem.get("goal", {})

    return jsonify({
        "btc": btc,
        "real_trades": real_trades,
        "bot_trade": bot_trade,
        "signals": signals[-20:][::-1],
        "stats": stats,
        "bot_stats": bot_stats,
        "user_total": round(user_total, 2),
        "bot_total": round(bot_total, 2),
        "total_withdrawn": mem.get("total_withdrawn", 0),
        "curve_user": curve_user,
        "curve_bot": curve_bot,
        "active_signal": mem.get("active_signal"),
        "goal": goal,
        "reflections": mem.get("bot_reflections", [])[-3:][::-1],
        "research_brief": mem.get("research_brief"),
        "bot_reflections": mem.get("bot_reflections", [])[-5:][::-1],
    })

@app.route("/api/open", methods=["POST"])
def api_open():
    data = request.json
    direction = data.get("direction")
    size = float(data.get("size", 0.1))
    symbol = data.get("symbol", "BTC")
    price = get_price("ETH-USDT" if symbol == "ETH" else "BTC-USDT")
    ep = float(data.get("price", 0)) or price
    if not ep:
        return jsonify({"ok": False, "msg": "获取价格失败"})
    sl_pts = 650
    sl_price = (ep + sl_pts) if direction == "做空" else (ep - sl_pts)
    tp_price = (ep - 1000) if direction == "做空" else (ep + 1000)
    now = datetime.now().strftime("%m-%d %H:%M")
    mem = load_memory()
    mem.setdefault("real_trades", [])
    # check add position
    for t in mem["real_trades"]:
        if t.get("symbol") == symbol and t.get("direction") == direction:
            old_size = t["size"]
            old_ep = t["entry_price"]
            new_size = round(old_size + size, 4)
            t["entry_price"] = round((old_ep * old_size + ep * size) / new_size, 2)
            t["size"] = new_size
            t["stop_loss"] = sl_price
            t["target"] = tp_price
            save_memory(mem)
            return jsonify({"ok": True, "msg": "加仓成功", "avg_price": t["entry_price"]})
    new_trade = {
        "symbol": symbol, "direction": direction,
        "entry_price": ep, "size": size,
        "stop_loss": sl_price, "target": tp_price,
        "targets": [{"price": tp_price, "label": "目标位", "hint": "止盈", "sources": ["固定1000点"]}],
        "stop_loss_points": sl_pts, "time": now, "status": "持仓中"
    }
    mem["real_trades"].append(new_trade)
    mem["real_trade"] = new_trade
    save_memory(mem)
    return jsonify({"ok": True, "msg": "开仓成功", "entry": ep})

@app.route("/api/close", methods=["POST"])
def api_close():
    data = request.json
    symbol = data.get("symbol", "BTC")
    mem = load_memory()
    price = get_price("ETH-USDT" if symbol == "ETH" else "BTC-USDT")
    trades = mem.get("real_trades", [])
    trade = None
    idx = None
    for i, t in enumerate(trades):
        if t.get("symbol", "BTC") == symbol:
            trade = t; idx = i; break
    if trade is None:
        return jsonify({"ok": False, "msg": "没有找到持仓"})
    ep = trade["entry_price"]
    size = trade.get("size", 0.1)
    if trade["direction"] == "做空":
        pnl = round((ep - price) * size, 2)
    else:
        pnl = round((price - ep) * size, 2)
    mem["stats"]["total"] = mem["stats"].get("total", 0) + 1
    if pnl >= 0:
        mem["stats"]["wins"] = mem["stats"].get("wins", 0) + 1
    else:
        mem["stats"]["losses"] = mem["stats"].get("losses", 0) + 1
    mem["stats"]["manual_closes"] = mem["stats"].get("manual_closes", 0) + 1
    mem["stats"]["total_pnl_usdt"] = mem["stats"].get("total_pnl_usdt", 0) + pnl
    mem["real_trades"].pop(idx)
    mem["real_trade"] = None
    save_memory(mem)
    return jsonify({"ok": True, "pnl": pnl, "close_price": price})

@app.route("/api/logs")
def api_logs():
    try:
        with open(LOG_FILE) as f:
            lines = f.readlines()
        return jsonify({"lines": lines[-50:]})
    except:
        return jsonify({"lines": []})

@app.route("/api/edit_trade", methods=["POST"])
def api_edit_trade():
    data = request.json
    symbol = data.get("symbol", "BTC")
    new_price = float(data.get("entry_price", 0))
    new_size = float(data.get("size", 0))
    if not new_price or not new_size:
        return jsonify({"ok": False, "msg": "参数不完整"})
    mem = load_memory()
    trades = mem.get("real_trades", [])
    for t in trades:
        if t.get("symbol", "BTC") == symbol:
            t["entry_price"] = new_price
            t["size"] = new_size
            mem["real_trade"] = t
            save_memory(mem)
            return jsonify({"ok": True, "msg": "修改成功"})
    return jsonify({"ok": False, "msg": "找不到该仓位"})

@app.route("/api/delete_trade", methods=["POST"])
def api_delete_trade():
    data = request.json
    symbol = data.get("symbol", "BTC")
    mem = load_memory()
    trades = mem.get("real_trades", [])
    new_trades = [t for t in trades if t.get("symbol", "BTC") != symbol]
    if len(new_trades) == len(trades):
        return jsonify({"ok": False, "msg": "找不到该仓位"})
    mem["real_trades"] = new_trades
    mem["real_trade"] = new_trades[-1] if new_trades else None
    save_memory(mem)
    return jsonify({"ok": True, "msg": "已删除"})


@app.route('/api/memory')
def api_memory():
    try:
        import json
        m = json.load(open('/Users/mac/.btc_monitor/memory.json'))
        return app.response_class(
            response=json.dumps(m, ensure_ascii=False),
            mimetype='application/json'
        )
    except Exception as e:
        return app.response_class(response='{"error":"%s"}' % str(e), mimetype='application/json')

@app.route("/api/kline")
def api_kline():
    symbol = request.args.get("symbol", "BTC-USDT")
    bar = request.args.get("bar", "15m")
    limit = int(request.args.get("limit", "200"))
    try:
        all_data = []
        after = None
        remaining = limit
        while remaining > 0:
            batch = min(remaining, 300)
            params = {"instId": symbol, "bar": bar, "limit": str(batch)}
            if after:
                params["after"] = str(after)
            r = requests.get(
                "https://www.okx.com/api/v5/market/candles",
                params=params, timeout=10, verify=False
            )
            data = r.json().get("data", [])
            if not data:
                break
            all_data.extend(data)
            after = data[-1][0]  # 最早一根的时间戳
            remaining -= len(data)
            if len(data) < batch:
                break
        return jsonify({"code": "0", "data": all_data})
    except Exception as e:
        return jsonify({"error": str(e), "data": []})

@app.route("/api/ticker")
def api_ticker():
    symbol = request.args.get("symbol", "BTC-USDT")
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/market/ticker",
            params={"instId": symbol},
            timeout=5, verify=False
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({"error": str(e), "data": []})

@app.route("/")
def index():
    return send_from_directory('.', 'lobster_animated_v8.html')

@app.route('/brain')
def brain():
    return send_from_directory('.', 'index.html')

@app.route('/<path:filename>')
def static_files(filename):
    return send_from_directory('.', filename)

if __name__ == "__main__":
    print("🦞 龙虾前端启动中...")
    print("   打开浏览器: http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)

