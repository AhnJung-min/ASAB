"""연결/인증 점검 스크립트.

실행: python test_connection.py
토큰 발급 -> 국내 시세 -> 해외 시세 -> 잔고 순으로 확인한다.
주문은 하지 않으므로 안전하다.
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔 한글 깨짐 방지

from src.kis.client import KISClient
from src.kis.config import load_config
from src.kis.domestic import DomesticStock
from src.kis.overseas import OverseasStock


def main() -> None:
    cfg = load_config()
    print(f"모드: {'모의투자' if cfg.paper_trading else '실전투자'}")
    print(f"base_url: {cfg.base_url}")

    client = KISClient(cfg)
    print("\n[1] 토큰 발급...")
    token = client.tokens.get_token()
    print(f"    OK (토큰 길이 {len(token)})")

    dom = DomesticStock(client)
    print("\n[2] 국내 시세 (삼성전자 005930)...")
    print(f"    현재가: {dom.current_price('005930'):,} 원")

    ovs = OverseasStock(client)
    print("\n[3] 해외 시세 (AAPL @ NAS)...")
    print(f"    현재가: ${ovs.current_price('NAS', 'AAPL')}")

    print("\n[4] 국내 잔고...")
    bal = dom.balance()
    print(f"    예수금: {bal['cash']:,} 원, 보유종목 {len(bal['holdings'])}개")
    for h in bal["holdings"]:
        print(f"      - {h['name']}({h['symbol']}) {h['qty']}주 / 평가손익 {h['pnl']:,}")

    print("\n모든 점검 완료 ✔")


if __name__ == "__main__":
    main()
