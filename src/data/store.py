"""SQLite 저장소. 일봉/투자자동향/재무/유니버스 스냅샷을 보관한다.

UPSERT(INSERT OR REPLACE)로 중복 수집해도 안전하게 갱신된다.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

DB_PATH = Path("data") / "market.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_price (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    open   INTEGER, high INTEGER, low INTEGER, close INTEGER,
    volume INTEGER, value INTEGER,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS investor_flow (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    foreign_qty     INTEGER,
    institution_qty INTEGER,
    individual_qty  INTEGER,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS financial_ratio (
    symbol TEXT NOT NULL,
    period TEXT NOT NULL,
    roe REAL, eps REAL, bps REAL,
    net_income_growth REAL, sales_growth REAL,
    PRIMARY KEY (symbol, period)
);
CREATE TABLE IF NOT EXISTS universe_snapshot (
    date   TEXT NOT NULL,
    rank   INTEGER,
    symbol TEXT NOT NULL,
    name   TEXT,
    price  INTEGER,
    change_pct REAL,
    volume INTEGER,
    value  INTEGER,
    PRIMARY KEY (date, symbol)
);
CREATE TABLE IF NOT EXISTS symbol_name (
    symbol TEXT PRIMARY KEY,
    name   TEXT
);
-- 종목 마스터 (ETF 제외 개별주 유니버스)
CREATE TABLE IF NOT EXISTS stock_master (
    symbol TEXT PRIMARY KEY,
    name   TEXT,
    market TEXT,        -- KOSPI | KOSDAQ
    group_code TEXT,    -- ST(주권) 등
    liquidity REAL      -- 시가총액(억원). 수집 우선순위/품질 기준
);
-- 계좌 자산 추이 (시간별 누적, 원화 기준)
CREATE TABLE IF NOT EXISTS account_snapshot (
    ts TEXT NOT NULL,
    cash_krw REAL, holdings_krw REAL, total_krw REAL,
    realized_krw REAL, unrealized_krw REAL, fx_rate REAL
);
CREATE INDEX IF NOT EXISTS idx_acct_ts ON account_snapshot(ts);
-- 수집 활동 로그 (대시보드 실시간 피드)
CREATE TABLE IF NOT EXISTS collect_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, kind TEXT, symbol TEXT, name TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_collect_log_id ON collect_log(id);
-- === 1군 분석 데이터 (실전 도메인 전용, 모델 피처용) =====================
-- 공매도 일별추이
CREATE TABLE IF NOT EXISTS short_sale (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    short_qty         INTEGER,  -- 공매도 체결 수량
    short_vol_ratio   REAL,     -- 공매도 거래량 비중(%)
    short_value       INTEGER,  -- 공매도 거래 대금
    short_value_ratio REAL,     -- 공매도 거래대금 비중(%)
    PRIMARY KEY (symbol, date)
);
-- 신용잔고 일별추이
CREATE TABLE IF NOT EXISTS credit_balance (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    loan_rmnd_qty   INTEGER,  -- 융자 잔고 주수
    loan_rmnd_rate  REAL,     -- 융자 잔고 비율
    loan_gvrt       REAL,     -- 융자 공여율
    stln_rmnd_qty   INTEGER,  -- 대주 잔고 주수
    PRIMARY KEY (symbol, date)
);
-- 종목별 프로그램매매추이(일별)
CREATE TABLE IF NOT EXISTS program_trade (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    prog_net_qty   INTEGER,  -- 프로그램 순매수 수량
    prog_net_value INTEGER,  -- 프로그램 순매수 대금
    PRIMARY KEY (symbol, date)
);
-- 종목별 일별 대차거래추이
CREATE TABLE IF NOT EXISTS loan_trans (
    symbol TEXT NOT NULL,
    date   TEXT NOT NULL,
    loan_new_qty    INTEGER,  -- 당일 증가 주수(체결)
    loan_redeem_qty INTEGER,  -- 당일 감소 주수(상환)
    loan_rmnd_qty   INTEGER,  -- 당일 잔고 주수
    loan_rmnd_amt   INTEGER,  -- 당일 잔고 금액
    PRIMARY KEY (symbol, date)
);
-- === 라이브 거래 저널 (자동매매 의사결정·주문 기록) =====================
-- 리밸런스 시점의 타깃/매매 신호 (모의 포워드 추적용)
CREATE TABLE IF NOT EXISTS live_signal (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,     -- 신호 생성 시각
    asof   TEXT,              -- 신호 산출 기준 데이터 날짜(YYYYMMDD)
    source TEXT,              -- screener_rotation 등 신호 출처
    symbol TEXT, name TEXT,
    rank   INTEGER, score REAL,
    action TEXT,              -- target | buy | sell | hold | skip
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_live_signal_id ON live_signal(id);
-- 실제(또는 모의/드라이런) 주문 기록
CREATE TABLE IF NOT EXISTS live_order (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    symbol   TEXT, name TEXT,
    side     TEXT,            -- buy | sell
    qty      INTEGER, price INTEGER, ord_dvsn TEXT,  -- ord_dvsn: 00 지정가/01 시장가
    mode     TEXT,            -- paper | real | dryrun
    status   TEXT,            -- sent | rejected | dryrun
    rt_cd    TEXT, msg TEXT, order_no TEXT
);
CREATE INDEX IF NOT EXISTS idx_live_order_id ON live_order(id);
-- 수출입동향(MOTIE 월간) 정형 지표 — 거시 패널/섹터 틸트용 (tidy: 월×지표×필드)
CREATE TABLE IF NOT EXISTS trade_stats (
    month  TEXT NOT NULL,    -- 'YYYY-MM'
    metric TEXT NOT NULL,    -- export|import|balance|반도체|자동차|... | mem_DDR4 ...
    field  TEXT NOT NULL,    -- usd_bil | yoy | price_usd
    value  REAL,
    PRIMARY KEY (month, metric, field)
);
-- 섹터별 월별 수출 시계열(관세청 HS CSV → 섹터 합산) — C(섹터 틸트) 검증용
CREATE TABLE IF NOT EXISTS export_history (
    month  TEXT NOT NULL,    -- 'YYYY-MM'
    sector TEXT NOT NULL,    -- 반도체|자동차|...
    export_kusd REAL,        -- 수출 금액(천불)
    PRIMARY KEY (month, sector)
);
-- 보유 포지션 상태(트레일링 고점 영속화 — --once 재시작에도 고점 유지)
CREATE TABLE IF NOT EXISTS live_position (
    symbol      TEXT PRIMARY KEY,
    name        TEXT,
    entry_price REAL,    -- 평균 매입가(잔고 기준 동기화)
    peak_price  REAL,    -- 보유 중 최고가(트레일링 기준)
    qty         INTEGER,
    opened_ts   TEXT,
    updated_ts  TEXT
);
-- 급등주 스캔 스냅샷 (국내 등락률 순위, 누적). 학습/분석용.
CREATE TABLE IF NOT EXISTS surge_scan (
    ts     TEXT NOT NULL,
    market TEXT, symbol TEXT, name TEXT,
    price  REAL, rate REAL, volume INTEGER
);
CREATE INDEX IF NOT EXISTS idx_surge_scan_ts ON surge_scan(ts);
-- 급등주 매매 기록 (진입~청산, 원화). "어떤 급등 패턴이 익절로 이어지나" 학습데이터.
CREATE TABLE IF NOT EXISTS surge_trade (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT, name TEXT, market TEXT,
    entry_ts TEXT, entry_price REAL, qty INTEGER,
    entry_rate REAL, entry_volume INTEGER,
    exit_ts TEXT, exit_price REAL,
    pnl REAL, pnl_pct REAL, reason TEXT, hold_sec INTEGER,
    status TEXT NOT NULL DEFAULT 'open'   -- open | closed
);
"""


class DataStore:
    def __init__(self, path: str | Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        # 동시 접근(수집기+대시보드+백테스트) 안정성
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._migrate()
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def _migrate(self) -> None:
        """구버전 account_snapshot(USD 컬럼)을 원화 스키마로 교체."""
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='account_snapshot'"
        )
        if cur.fetchone():
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(account_snapshot)")}
            if "cash_krw" not in cols:  # 구 스키마
                self.conn.execute("DROP TABLE account_snapshot")
                self.conn.commit()
        # stock_master.liquidity 컬럼 추가(구 DB 보강)
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='stock_master'")
        if cur.fetchone():
            mcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(stock_master)")}
            if "liquidity" not in mcols:
                self.conn.execute("ALTER TABLE stock_master ADD COLUMN liquidity REAL")
                self.conn.commit()
        # 구 surge 테이블(미국 트랙, exchange 컬럼/USD)을 국내(market/원화) 스키마로 교체.
        # 폐기된 미국 데이터라 드롭 후 재생성한다.
        for tbl in ("surge_trade", "surge_scan"):
            cur = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tbl,))
            if cur.fetchone():
                cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({tbl})")}
                if "market" not in cols:  # 구 스키마(exchange)
                    self.conn.execute(f"DROP TABLE {tbl}")
                    self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "DataStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- 쓰기 ---------------------------------------------------------------
    def save_daily(self, symbol: str, rows: Iterable[dict[str, Any]]) -> int:
        data = [
            (symbol, r["date"], r["open"], r["high"], r["low"], r["close"],
             r["volume"], r["value"])
            for r in rows
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO daily_price "
            "(symbol,date,open,high,low,close,volume,value) "
            "VALUES (?,?,?,?,?,?,?,?)",
            data,
        )
        self.conn.commit()
        return len(data)

    def save_investor(self, symbol: str, rows: Iterable[dict[str, Any]]) -> int:
        data = [
            (symbol, r["date"], r["foreign"], r["institution"], r["individual"])
            for r in rows
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO investor_flow "
            "(symbol,date,foreign_qty,institution_qty,individual_qty) "
            "VALUES (?,?,?,?,?)",
            data,
        )
        self.conn.commit()
        return len(data)

    def save_financial(self, symbol: str, r: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO financial_ratio "
            "(symbol,period,roe,eps,bps,net_income_growth,sales_growth) "
            "VALUES (?,?,?,?,?,?,?)",
            (symbol, r["period"], r["roe"], r["eps"], r["bps"],
             r["net_income_growth"], r["sales_growth"]),
        )
        self.conn.commit()

    # --- 1군 분석 데이터 저장 -----------------------------------------------
    def save_short_sale(self, symbol: str, rows: Iterable[dict[str, Any]]) -> int:
        data = [
            (symbol, r["date"], r["short_qty"], r["short_vol_ratio"],
             r["short_value"], r["short_value_ratio"])
            for r in rows
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO short_sale "
            "(symbol,date,short_qty,short_vol_ratio,short_value,short_value_ratio) "
            "VALUES (?,?,?,?,?,?)", data)
        self.conn.commit()
        return len(data)

    def save_credit_balance(self, symbol: str, rows: Iterable[dict[str, Any]]) -> int:
        data = [
            (symbol, r["date"], r["loan_rmnd_qty"], r["loan_rmnd_rate"],
             r["loan_gvrt"], r["stln_rmnd_qty"])
            for r in rows
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO credit_balance "
            "(symbol,date,loan_rmnd_qty,loan_rmnd_rate,loan_gvrt,stln_rmnd_qty) "
            "VALUES (?,?,?,?,?,?)", data)
        self.conn.commit()
        return len(data)

    def save_program_trade(self, symbol: str, rows: Iterable[dict[str, Any]]) -> int:
        data = [
            (symbol, r["date"], r["prog_net_qty"], r["prog_net_value"])
            for r in rows
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO program_trade "
            "(symbol,date,prog_net_qty,prog_net_value) VALUES (?,?,?,?)", data)
        self.conn.commit()
        return len(data)

    def save_loan_trans(self, symbol: str, rows: Iterable[dict[str, Any]]) -> int:
        data = [
            (symbol, r["date"], r["loan_new_qty"], r["loan_redeem_qty"],
             r["loan_rmnd_qty"], r["loan_rmnd_amt"])
            for r in rows
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO loan_trans "
            "(symbol,date,loan_new_qty,loan_redeem_qty,loan_rmnd_qty,loan_rmnd_amt) "
            "VALUES (?,?,?,?,?,?)", data)
        self.conn.commit()
        return len(data)

    def save_universe(self, date: str, rows: Iterable[dict[str, Any]]) -> int:
        rows = list(rows)
        data = [
            (date, r.get("rank"), r["symbol"], r["name"], r["price"],
             r["change_pct"], r["volume"], r["value"])
            for r in rows
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO universe_snapshot "
            "(date,rank,symbol,name,price,change_pct,volume,value) "
            "VALUES (?,?,?,?,?,?,?,?)",
            data,
        )
        self.conn.executemany(
            "INSERT OR REPLACE INTO symbol_name (symbol,name) VALUES (?,?)",
            [(r["symbol"], r["name"]) for r in rows],
        )
        self.conn.commit()
        return len(data)

    # --- 종목 마스터 --------------------------------------------------------
    def save_master(self, stocks: Iterable[dict[str, Any]]) -> int:
        rows = [(s["symbol"], s["name"], s["market"], s.get("group", "ST")) for s in stocks]
        self.conn.executemany(
            "INSERT OR REPLACE INTO stock_master (symbol,name,market,group_code) "
            "VALUES (?,?,?,?)", rows,
        )
        # 이름 조회 테이블에도 반영
        self.conn.executemany(
            "INSERT OR REPLACE INTO symbol_name (symbol,name) VALUES (?,?)",
            [(s["symbol"], s["name"]) for s in stocks],
        )
        self.conn.commit()
        return len(rows)

    def master_symbols(self, market: str | None = None,
                       by_liquidity: bool = False) -> list[dict[str, Any]]:
        # 유동성(시가총액) 내림차순: NULL(미조사)은 뒤로
        order = "ORDER BY liquidity IS NULL, liquidity DESC" if by_liquidity else "ORDER BY symbol"
        if market:
            cur = self.conn.execute(
                f"SELECT symbol,name,market,liquidity FROM stock_master WHERE market=? {order}",
                (market,))
        else:
            cur = self.conn.execute(
                f"SELECT symbol,name,market,liquidity FROM stock_master {order}")
        return [dict(r) for r in cur.fetchall()]

    def set_master_liquidity(self, values: list[tuple[float, str]]) -> int:
        """[(liquidity, symbol), ...] 일괄 갱신."""
        self.conn.executemany(
            "UPDATE stock_master SET liquidity=? WHERE symbol=?", values)
        self.conn.commit()
        return len(values)

    # --- 수집 활동 로그 -----------------------------------------------------
    def add_collect_log(self, ts: str, kind: str, symbol: str,
                        name: str, detail: str) -> None:
        self.conn.execute(
            "INSERT INTO collect_log (ts,kind,symbol,name,detail) VALUES (?,?,?,?,?)",
            (ts, kind, symbol, name, detail))
        self.conn.commit()

    def recent_collect_log(self, limit: int = 30) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT ts,kind,symbol,name,detail FROM collect_log "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def save_account_snapshot(self, ts: str, s: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO account_snapshot "
            "(ts,cash_krw,holdings_krw,total_krw,realized_krw,unrealized_krw,fx_rate) "
            "VALUES (?,?,?,?,?,?,?)",
            (ts, s["cash_krw"], s["holdings_krw"], s["total_krw"],
             s["realized_krw"], s["unrealized_krw"], s["fx_rate"]),
        )
        self.conn.commit()

    def account_snapshots(self, limit: int = 500) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM (SELECT * FROM account_snapshot ORDER BY ts DESC LIMIT ?) "
            "ORDER BY ts", (limit,)
        ).fetchall()

    # --- 수출입동향(거시) -----------------------------------------------------
    def save_trade_stats(self, d: dict[str, Any]) -> int:
        """parse_pdf() 결과(dict)를 tidy 행으로 펼쳐 UPSERT. 적재 행수 반환."""
        month = d.get("month")
        rows: list[tuple] = []
        for metric in ("export", "import", "balance"):
            for field in ("usd_bil", "yoy"):
                key = f"{metric}_{field}" if metric != "balance" else "balance_usd_bil"
                if metric == "balance" and field == "yoy":
                    continue
                if key in d:
                    rows.append((month, metric, field, float(d[key])))
        for name, v in d.get("items", {}).items():
            rows.append((month, name, "usd_bil", float(v["usd_bil"])))
            rows.append((month, name, "yoy", float(v["yoy"])))
        for name, v in d.get("memory", {}).items():
            rows.append((month, f"mem_{name}", "price_usd", float(v["price_usd"])))
            rows.append((month, f"mem_{name}", "yoy", float(v["yoy"])))
        self.conn.executemany(
            "INSERT INTO trade_stats (month,metric,field,value) VALUES (?,?,?,?) "
            "ON CONFLICT(month,metric,field) DO UPDATE SET value=excluded.value", rows)
        self.conn.commit()
        return len(rows)

    def get_trade_stats(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT month,metric,field,value FROM trade_stats ORDER BY month").fetchall()

    def save_export_history(self, rows: list[dict[str, Any]]) -> int:
        data = [(r["month"], r["sector"], float(r["export_kusd"])) for r in rows]
        self.conn.executemany(
            "INSERT INTO export_history (month,sector,export_kusd) VALUES (?,?,?) "
            "ON CONFLICT(month,sector) DO UPDATE SET export_kusd=excluded.export_kusd", data)
        self.conn.commit()
        return len(data)

    def get_export_history(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT month,sector,export_kusd FROM export_history ORDER BY month,sector").fetchall()

    # --- 라이브 거래 저널 ---------------------------------------------------
    def add_live_signals(self, ts: str, source: str, asof: str | None,
                         rows: Iterable[dict[str, Any]]) -> int:
        data = [
            (ts, asof, source, r["symbol"], r.get("name"),
             r.get("rank"), r.get("score"), r["action"], r.get("reason"))
            for r in rows
        ]
        self.conn.executemany(
            "INSERT INTO live_signal "
            "(ts,asof,source,symbol,name,rank,score,action,reason) "
            "VALUES (?,?,?,?,?,?,?,?,?)", data)
        self.conn.commit()
        return len(data)

    def add_live_order(self, o: dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO live_order "
            "(ts,symbol,name,side,qty,price,ord_dvsn,mode,status,rt_cd,msg,order_no) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (o["ts"], o["symbol"], o.get("name"), o["side"], o["qty"],
             o.get("price", 0), o.get("ord_dvsn"), o.get("mode"), o.get("status"),
             o.get("rt_cd"), o.get("msg"), o.get("order_no")))
        self.conn.commit()
        return cur.lastrowid

    def recent_live_signals(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM live_signal ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def recent_live_orders(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM live_order ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # --- 보유 포지션 상태(트레일링 고점) ----------------------------------
    def get_positions(self) -> dict[str, dict[str, Any]]:
        return {r["symbol"]: dict(r)
                for r in self.conn.execute("SELECT * FROM live_position")}

    def save_position(self, symbol: str, name: str, entry_price: float,
                      peak_price: float, qty: int, opened_ts: str, ts: str) -> None:
        self.conn.execute(
            "INSERT INTO live_position "
            "(symbol,name,entry_price,peak_price,qty,opened_ts,updated_ts) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET "
            "name=excluded.name, entry_price=excluded.entry_price, "
            "peak_price=excluded.peak_price, qty=excluded.qty, updated_ts=excluded.updated_ts",
            (symbol, name, entry_price, peak_price, qty, opened_ts, ts))
        self.conn.commit()

    def delete_position(self, symbol: str) -> None:
        self.conn.execute("DELETE FROM live_position WHERE symbol=?", (symbol,))
        self.conn.commit()

    # --- 급등주 스캔/매매(국내 단타 트랙) ---------------------------------
    def save_surge_scan(self, ts: str, rows: Iterable[dict[str, Any]]) -> int:
        data = [(ts, r.get("market", ""), r["symbol"], r["name"],
                 r["price"], r["rate"], r["volume"]) for r in rows]
        self.conn.executemany(
            "INSERT INTO surge_scan (ts,market,symbol,name,price,rate,volume) "
            "VALUES (?,?,?,?,?,?,?)", data)
        self.conn.commit()
        return len(data)

    def open_trade(self, t: dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO surge_trade "
            "(symbol,name,market,entry_ts,entry_price,qty,entry_rate,entry_volume,status) "
            "VALUES (?,?,?,?,?,?,?,?,'open')",
            (t["symbol"], t["name"], t.get("market", ""), t["entry_ts"],
             t["entry_price"], t["qty"], t.get("entry_rate"), t.get("entry_volume")))
        self.conn.commit()
        return cur.lastrowid

    def close_trade(self, trade_id: int, exit_ts: str, exit_price: float,
                    pnl: float, pnl_pct: float, reason: str, hold_sec: int) -> None:
        self.conn.execute(
            "UPDATE surge_trade SET exit_ts=?, exit_price=?, pnl=?, pnl_pct=?, "
            "reason=?, hold_sec=?, status='closed' WHERE id=?",
            (exit_ts, exit_price, pnl, pnl_pct, reason, hold_sec, trade_id))
        self.conn.commit()

    def open_trades(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM surge_trade WHERE status='open'").fetchall()

    def recent_trades(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM surge_trade ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def recent_surge_scans(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM surge_scan ORDER BY ts DESC, rate DESC LIMIT ?",
            (limit,)).fetchall()

    # --- 읽기 ---------------------------------------------------------------
    def latest_date(self, symbol: str) -> str | None:
        cur = self.conn.execute(
            "SELECT MAX(date) AS d FROM daily_price WHERE symbol=?", (symbol,)
        )
        return cur.fetchone()["d"]

    def get_daily(self, symbol: str) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM daily_price WHERE symbol=? ORDER BY date", (symbol,)
        )
        return cur.fetchall()

    def symbols(self) -> list[str]:
        cur = self.conn.execute("SELECT DISTINCT symbol FROM daily_price ORDER BY symbol")
        return [r["symbol"] for r in cur.fetchall()]

    def name_of(self, symbol: str) -> str:
        cur = self.conn.execute("SELECT name FROM symbol_name WHERE symbol=?", (symbol,))
        row = cur.fetchone()
        return row["name"] if row else symbol

    def stats(self) -> dict[str, int]:
        out = {}
        for tbl in ("daily_price", "investor_flow", "financial_ratio", "universe_snapshot",
                    "short_sale", "credit_balance", "program_trade", "loan_trans"):
            out[tbl] = self.conn.execute(f"SELECT COUNT(*) AS c FROM {tbl}").fetchone()["c"]
        out["symbols"] = len(self.symbols())
        return out
