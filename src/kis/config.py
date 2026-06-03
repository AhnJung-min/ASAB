"""설정 로딩. config.yaml 을 읽어 dataclass 로 제공한다."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# 도메인 (모의투자 / 실전투자)
PAPER_BASE_URL = "https://openapivts.koreainvestment.com:29443"
REAL_BASE_URL = "https://openapi.koreainvestment.com:9443"
PAPER_WS_URL = "ws://ops.koreainvestment.com:31000"
REAL_WS_URL = "ws://ops.koreainvestment.com:21000"


@dataclass
class Config:
    paper_trading: bool
    app_key: str
    app_secret: str
    account_no: str
    account_product_code: str
    initial_capital_krw: int = 10_000_000
    trading: dict[str, Any] = field(default_factory=dict)
    strategy: dict[str, Any] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return PAPER_BASE_URL if self.paper_trading else REAL_BASE_URL

    @property
    def ws_url(self) -> str:
        return PAPER_WS_URL if self.paper_trading else REAL_WS_URL


def load_config(path: str | Path = "config.yaml") -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"설정 파일이 없습니다: {p}\n"
            "config.yaml.example 을 config.yaml 로 복사하고 값을 채워주세요."
        )
    data = yaml.safe_load(p.read_text(encoding="utf-8"))

    missing = [k for k in ("app_key", "app_secret", "account_no") if not data.get(k)]
    if missing:
        raise ValueError(f"config.yaml 에 다음 값이 비어 있습니다: {', '.join(missing)}")

    return Config(
        paper_trading=bool(data.get("paper_trading", True)),
        app_key=str(data["app_key"]).strip(),
        app_secret=str(data["app_secret"]).strip(),
        account_no=str(data["account_no"]).strip(),
        account_product_code=str(data.get("account_product_code", "01")).strip(),
        initial_capital_krw=int(data.get("initial_capital_krw", 10_000_000)),
        trading=data.get("trading", {}) or {},
        strategy=data.get("strategy", {}) or {},
    )
