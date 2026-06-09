"""ASAB 단타 웹 대시보드 — Flask 백엔드 (API + 정적 프론트엔드).

실행:  python -m web.app        (또는  python web/app.py)
브라우저:  http://localhost:8000

data/market.db 를 읽기 전용으로 조회해 JSON API 로 제공한다.
봇이 DB에 쓰는 동안에도 안전(WAL, 읽기만).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "market.db"
MODEL_PATH = ROOT / "data" / "surge_model.pkl"

app = Flask(__name__)


def db():
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _today(con) -> str:
    """데이터상의 최신 거래일(YYYY-MM-DD). 스캔이 없으면 시스템 날짜."""
    r = con.execute("SELECT MAX(substr(ts,1,10)) d FROM surge_scan").fetchone()
    if r and r["d"]:
        return r["d"]
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d")


def _q(con, sql, params=()):
    return [dict(r) for r in con.execute(sql, params).fetchall()]


# ---------------- API ----------------
@app.route("/api/overview")
def overview():
    con = db()
    try:
        today = _today(con)
        like = today + "%"
        ymd = today.replace("-", "")

        # 계좌 최신
        acct = con.execute(
            "SELECT cash_krw,holdings_krw,total_krw,unrealized_krw FROM account_snapshot "
            "ORDER BY ts DESC LIMIT 1").fetchone()
        total = acct["total_krw"] if acct else 0
        unreal = acct["unrealized_krw"] if acct else 0

        # 오늘 매매
        closed = con.execute(
            "SELECT COUNT(*) n, SUM(pnl) pnl, "
            "SUM(CASE WHEN reason='익절' THEN 1 ELSE 0 END) win, "
            "AVG(pnl_pct) avgpct "
            "FROM surge_trade WHERE status='closed' AND entry_ts LIKE ?", (like,)).fetchone()
        n_closed = closed["n"] or 0
        realized = closed["pnl"] or 0
        winrate = (closed["win"] / n_closed * 100) if n_closed else 0

        opn = con.execute("SELECT COUNT(*) n FROM surge_trade WHERE status='open'").fetchone()["n"]
        scans = con.execute("SELECT COUNT(*) n FROM surge_scan WHERE ts LIKE ?", (like,)).fetchone()["n"]
        reb = con.execute(
            "SELECT COUNT(DISTINCT symbol) n FROM surge_scan WHERE source='rebound' AND ts LIKE ?",
            (like,)).fetchone()["n"]
        mbars = con.execute("SELECT COUNT(*) n FROM minute_bar WHERE date=?", (ymd,)).fetchone()["n"]

        # 청산 사유 분포
        reasons = _q(con,
            "SELECT reason, COUNT(*) n FROM surge_trade WHERE status='closed' AND entry_ts LIKE ? "
            "GROUP BY reason", (like,))

        # 모델 메타
        model = _model_meta()

        return jsonify({
            "today": today,
            "total_krw": total, "unrealized_krw": unreal, "realized_krw": realized,
            "open_positions": opn, "closed_today": n_closed, "winrate": round(winrate, 1),
            "scans_today": scans, "rebound_symbols": reb, "minute_bars": mbars,
            "reasons": reasons, "model": model,
        })
    finally:
        con.close()


def _model_meta():
    if not MODEL_PATH.exists():
        return None
    try:
        import pickle
        with open(MODEL_PATH, "rb") as f:
            b = pickle.load(f)
        m = b.get("meta", {})
        return {"rows": m.get("rows"), "kind": m.get("kind"), "oos_ic": m.get("oos_ic"),
                "mean_fwd_net": m.get("mean_fwd_net"), "horizon_min": m.get("horizon_min")}
    except Exception:
        return None


@app.route("/api/account_curve")
def account_curve():
    con = db()
    try:
        rows = _q(con,
            "SELECT ts, total_krw FROM (SELECT * FROM account_snapshot ORDER BY ts DESC LIMIT 400) "
            "ORDER BY ts")
        return jsonify(rows)
    finally:
        con.close()


@app.route("/api/positions")
def positions():
    con = db()
    try:
        rows = _q(con,
            "SELECT name,symbol,qty,entry_price,entry_rate,entry_ts,entry_ob_imbalance "
            "FROM surge_trade WHERE status='open' ORDER BY entry_ts DESC")
        return jsonify(rows)
    finally:
        con.close()


@app.route("/api/trades")
def trades():
    con = db()
    try:
        rows = _q(con,
            "SELECT name,symbol,entry_price,exit_price,qty,pnl,pnl_pct,reason,entry_ts,exit_ts,hold_sec "
            "FROM surge_trade WHERE status='closed' ORDER BY id DESC LIMIT 50")
        return jsonify(rows)
    finally:
        con.close()


@app.route("/api/activity")
def activity():
    """시간대별 매수/청산 + 스캔 건수."""
    con = db()
    try:
        like = _today(con) + "%"
        buys = _q(con,
            "SELECT substr(entry_ts,12,2) hh, COUNT(*) n FROM surge_trade "
            "WHERE entry_ts LIKE ? GROUP BY hh ORDER BY hh", (like,))
        scans = _q(con,
            "SELECT substr(ts,12,2) hh, COUNT(DISTINCT ts) n FROM surge_scan "
            "WHERE ts LIKE ? GROUP BY hh ORDER BY hh", (like,))
        return jsonify({"buys": buys, "scans": scans})
    finally:
        con.close()


@app.route("/api/scan_latest")
def scan_latest():
    """가장 최근 스캔의 후보(상승/하락) 일부."""
    con = db()
    try:
        last = con.execute("SELECT MAX(ts) m FROM surge_scan").fetchone()["m"]
        rows = _q(con,
            "SELECT name,symbol,rate,volume,source FROM surge_scan WHERE ts=? "
            "ORDER BY rate DESC LIMIT 60", (last,))
        return jsonify({"ts": last, "rows": rows})
    finally:
        con.close()


@app.route("/api/symbols")
def symbols():
    con = db()
    try:
        ymd = _today(con).replace("-", "")
        rows = _q(con,
            "SELECT m.symbol, COALESCE(s.name, m.symbol) name FROM "
            "(SELECT DISTINCT symbol FROM minute_bar WHERE date=?) m "
            "LEFT JOIN symbol_name s ON s.symbol=m.symbol ORDER BY name", (ymd,))
        return jsonify(rows)
    finally:
        con.close()


@app.route("/api/minute/<sym>")
def minute(sym):
    con = db()
    try:
        ymd = _today(con).replace("-", "")
        rows = _q(con,
            "SELECT time, close, high, low, volume FROM minute_bar "
            "WHERE symbol=? AND date=? ORDER BY time", (sym, ymd))
        name = con.execute("SELECT name FROM symbol_name WHERE symbol=?", (sym,)).fetchone()
        # 이 종목 매매 마커
        tr = _q(con,
            "SELECT entry_ts,exit_ts,entry_price,exit_price,reason,pnl_pct FROM surge_trade "
            "WHERE symbol=? ORDER BY id", (sym,))
        return jsonify({"symbol": sym, "name": name["name"] if name else sym,
                        "bars": rows, "trades": tr})
    finally:
        con.close()


@app.route("/api/rebound")
def rebound():
    """오늘 하락(-%) 스캔된 종목 + 분봉상 저점 후 반등폭(후견지명, 참고용)."""
    con = db()
    try:
        like = _today(con) + "%"; ymd = _today(con).replace("-", "")
        syms = _q(con,
            "SELECT symbol, name, MIN(rate) minrate FROM surge_scan "
            "WHERE source='rebound' AND ts LIKE ? GROUP BY symbol ORDER BY minrate", (like,))
        out = []
        for s in syms[:40]:
            bars = con.execute(
                "SELECT low, close FROM minute_bar WHERE symbol=? AND date=? ORDER BY time",
                (s["symbol"], ymd)).fetchall()
            bounce = None
            if len(bars) > 20:
                lows = [b["low"] for b in bars]; closes = [b["close"] for b in bars]
                lo = min(lows); li = lows.index(lo)
                after = closes[li:]
                if lo > 0 and after:
                    bounce = round((max(after) / lo - 1) * 100, 1)
            out.append({**s, "bounce_pct": bounce})
        return jsonify(out)
    finally:
        con.close()


@app.route("/")
def index():
    return render_template("index.html")


if __name__ == "__main__":
    print("ASAB 단타 대시보드 → http://localhost:8000")
    app.run(host="0.0.0.0", port=8000, debug=False)
