#!/usr/bin/env python3
"""
BTC 龙虾研究助手 v1.0
- 每30分钟运行一次（慢思考）
- Tavily 搜索宏观新闻 + 社交情绪
- Polymarket 预测市场数据
- 多视角分析（技术派/基本面派/情绪派）
- 输出市场研判摘要 → 写入 memory.json 供龙虾读取
"""

import json, os, time, requests
from datetime import datetime
import urllib3
urllib3.disable_warnings()

# ── 配置 ─────────────────────────────────────────────
MEMORY_DIR   = os.path.expanduser("~/.btc_monitor")
MEMORY_FILE  = os.path.join(MEMORY_DIR, "memory.json")
LOG_FILE     = os.path.join(MEMORY_DIR, "research.log")

TAVILY_API_KEY    = "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"
OPENROUTER_API_KEY = "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"
TELEGRAM_BOT_TOKEN = "7700344017:AAFiPbMDGVJpA_h5CKMOuDXfMPpqUJ5Q7Ek"
TELEGRAM_CHAT_ID   = "1715750977"

os.environ["https_proxy"] = "http://127.0.0.1:7890"
os.environ["http_proxy"]  = "http://127.0.0.1:7890"

# ── 工具函数 ─────────────────────────────────────────
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[{}] {}".format(ts, msg)
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except:
        pass

def load_memory():
    try:
        with open(MEMORY_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_memory(m):
    with open(MEMORY_FILE, "w") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)

def send_telegram(msg):
    try:
        requests.post(
            "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_BOT_TOKEN),
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except:
        pass

def claude_request(prompt, max_tokens=800, model="anthropic/claude-sonnet-4-5"):
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": "Bearer {}".format(OPENROUTER_API_KEY),
                     "Content-Type": "application/json"},
            json={"model": model,
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": max_tokens},
            timeout=60
        )
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log("Claude请求失败: {}".format(e))
        return None

# ── 数据获取 ─────────────────────────────────────────
def get_btc_price():
    try:
        r = requests.get("https://www.okx.com/api/v5/market/ticker",
                         params={"instId": "BTC-USDT"}, timeout=8, verify=False)
        d = r.json()["data"][0]
        return {
            "price": float(d["last"]),
            "change_24h": float(d["chg24h"]) * 100 if d.get("chg24h") else 0,
            "vol_24h": float(d["volCcy24h"]) if d.get("volCcy24h") else 0,
            "high_24h": float(d["high24h"]),
            "low_24h": float(d["low24h"]),
        }
    except Exception as e:
        log("价格获取失败: {}".format(e))
        return None

def tavily_search(query, max_results=5):
    """Tavily 搜索"""
    try:
        r = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
                "include_answer": True,
                "include_raw_content": False,
            },
            timeout=20
        )
        data = r.json()
        answer  = data.get("answer", "")
        results = data.get("results", [])
        snippets = [res.get("content", "")[:200] for res in results[:3]]
        return answer, snippets
    except Exception as e:
        log("Tavily搜索失败: {}".format(e))
        return "", []

def get_polymarket_btc():
    """获取Polymarket上BTC相关预测市场数据"""
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "tag": "crypto", "limit": "20"},
            timeout=15
        )
        markets = r.json()
        btc_markets = []
        for m in markets:
            try:
                title = m.get("question", m.get("title", ""))
                if "BTC" not in title.upper() and "BITCOIN" not in title.upper():
                    continue
                outcomes = m.get("outcomePrices", [])
                # outcomes 可能是字符串列表 ["0.8", "0.2"] 或已经是数字
                if isinstance(outcomes, str):
                    import json as _json
                    outcomes = _json.loads(outcomes)
                yes_prob = float(outcomes[0]) * 100 if outcomes else 0
                no_prob  = float(outcomes[1]) * 100 if len(outcomes) > 1 else 0
                btc_markets.append({
                    "question": title[:80],
                    "yes_prob": round(yes_prob, 1),
                    "no_prob":  round(no_prob, 1),
                    "volume":   float(m.get("volumeNum", 0)),
                })
            except:
                continue
        return btc_markets[:5]
    except Exception as e:
        log("Polymarket获取失败: {}".format(e))
        return []

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        d = r.json()["data"][0]
        return {"value": int(d["value"]), "label": d["value_classification"]}
    except:
        return None

# ── 多视角分析 ────────────────────────────────────────
def multi_perspective_analysis(price_data, news_answer, social_answer, polymarket, fg):
    """
    三个视角分析BTC行情，最后综合裁判
    技术派 + 基本面派 + 情绪派
    """
    price   = price_data["price"]
    chg_24h = price_data["change_24h"]
    vol_24h = price_data["vol_24h"]
    h24     = price_data["high_24h"]
    l24     = price_data["low_24h"]

    # Polymarket摘要
    poly_str = "无数据"
    if polymarket:
        poly_str = " | ".join([
            "{} 是:{:.0f}%".format(m["question"][:30], m["yes_prob"])
            for m in polymarket[:3]
        ])

    fg_str = "恐惧贪婪: {}/100 ({})".format(fg["value"], fg["label"]) if fg else "无数据"

    prompt = (
        "你是BTC合约交易研究员，现在用三个视角分析当前行情，然后给出综合研判。\n\n"
        "【当前数据】\n"
        "价格: ${:,.0f} | 24H涨跌: {:+.2f}% | 24H高低: ${:,.0f}-${:,.0f}\n"
        "24H成交量: {:.0f} BTC\n"
        "{}\n"
        "宏观新闻摘要: {}\n"
        "市场情绪: {}\n"
        "Polymarket预测: {}\n\n"
        "请严格按以下格式输出，每个视角2句话，综合研判3句话：\n\n"
        "【技术派】\n"
        "（从价格位置、波动、成交量判断短线方向）\n\n"
        "【基本面派】\n"
        "（从宏观新闻、政策、市场事件判断影响）\n\n"
        "【情绪派】\n"
        "（从恐惧贪婪、社交情绪、Polymarket判断市场心理）\n\n"
        "【综合研判】\n"
        "（整合三个视角，给出方向判断：看多/看空/中性，以及最需要关注的风险）\n\n"
        "【结论】做多/做空/观望\n"
        "【置信度】高/中/低\n"
        "【核心逻辑】一句话"
    ).format(
        price, chg_24h, h24, l24, vol_24h,
        fg_str, news_answer[:200], social_answer[:150], poly_str
    )

    return claude_request(prompt, max_tokens=600)

# ── 主流程 ────────────────────────────────────────────
def run_research():
    log("="*50)
    log("龙虾研究助手开始分析...")

    # 1. 获取价格数据
    price_data = get_btc_price()
    if not price_data:
        log("价格获取失败，跳过本轮")
        return
    log("BTC: ${:,.0f} ({:+.2f}%)".format(price_data["price"], price_data["change_24h"]))

    # 2. Tavily 搜索
    log("搜索宏观新闻...")
    news_q = "Bitcoin BTC crypto market news today {}".format(
        datetime.now().strftime("%Y-%m-%d"))
    news_answer, news_snippets = tavily_search(news_q, max_results=5)

    log("搜索市场情绪...")
    social_q = "Bitcoin BTC sentiment social media crypto trader {}".format(
        datetime.now().strftime("%Y-%m-%d"))
    social_answer, _ = tavily_search(social_q, max_results=3)

    log("搜索宏观事件...")
    macro_q = "Federal Reserve Fed interest rate inflation crypto market {}".format(
        datetime.now().strftime("%Y-%m"))
    macro_answer, _ = tavily_search(macro_q, max_results=3)

    # 3. Polymarket
    log("获取Polymarket预测...")
    polymarket = get_polymarket_btc()
    log("Polymarket: {}个BTC相关市场".format(len(polymarket)))

    # 4. 恐惧贪婪
    fg = get_fear_greed()

    # 5. 多视角分析
    combined_news = "{} | 宏观: {}".format(news_answer[:150], macro_answer[:100])
    log("多视角分析中...")
    analysis = multi_perspective_analysis(
        price_data, combined_news, social_answer, polymarket, fg)

    if not analysis:
        log("分析失败")
        return

    log("分析完成:\n{}".format(analysis[:200]))

    # 6. 解析结论
    conclusion = "观望"
    confidence = "中"
    core_logic = ""
    for line in analysis.split("\n"):
        if "【结论】" in line:
            if "做多" in line: conclusion = "做多"
            elif "做空" in line: conclusion = "做空"
            else: conclusion = "观望"
        if "【置信度】" in line:
            if "高" in line: confidence = "高"
            elif "低" in line: confidence = "低"
        if "【核心逻辑】" in line:
            core_logic = line.replace("【核心逻辑】", "").strip()

    # 7. 写入memory
    memory = load_memory()
    now = datetime.now().strftime("%m-%d %H:%M")
    memory["research_brief"] = {
        "time": now,
        "price": price_data["price"],
        "change_24h": price_data["change_24h"],
        "conclusion": conclusion,
        "confidence": confidence,
        "core_logic": core_logic,
        "full_analysis": analysis[:800],
        "news_summary": news_answer[:200],
        "social_summary": social_answer[:150],
        "macro_summary": macro_answer[:150],
        "polymarket": polymarket[:3],
        "fear_greed": fg,
    }
    save_memory(memory)
    log("研判结果已写入memory: {} {}".format(conclusion, confidence))

    # 8. 推送TG（只在结论是做多/做空且置信度高时）
    if confidence == "高" or (conclusion != "观望"):
        emoji = "🟢" if conclusion == "做多" else "🔴" if conclusion == "做空" else "⚪"
        msg = "{} <b>龙虾研究助手研判</b> {}\n".format(emoji, now)
        msg += "─────────────────────\n"
        msg += "BTC: ${:,.0f} ({:+.2f}%)\n".format(
            price_data["price"], price_data["change_24h"])
        msg += "结论: <b>{}</b> | 置信度: {}\n".format(conclusion, confidence)
        msg += "核心逻辑: {}\n\n".format(core_logic)
        if fg:
            msg += "恐惧贪婪: {}/100 ({})\n".format(fg["value"], fg["label"])
        if polymarket:
            msg += "\nPolymarket:\n"
            for m in polymarket[:2]:
                msg += "  {} 是:{:.0f}%\n".format(m["question"][:40], m["yes_prob"])
        send_telegram(msg)
        log("已推送TG")

    log("本轮分析完成")

if __name__ == "__main__":
    log("龙虾研究助手启动")
    while True:
        try:
            run_research()
        except Exception as e:
            log("研究助手异常: {}".format(e))
        log("等待30分钟...")
        time.sleep(1800)
