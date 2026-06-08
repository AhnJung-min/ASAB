"""ASAB 통합 실행 메뉴 — 흩어진 .bat 들을 하나로 합친 콘솔 런처.

실행:  ASAB.bat 더블클릭  (또는  py -m src.menu)
번호를 입력하면 해당 작업을 실행하고, 끝나면(또는 Ctrl+C 로 중지하면) 메뉴로 돌아온다.
"""
from __future__ import annotations

import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔 한글 깨짐 방지

PY = sys.executable  # 이 메뉴를 띄운 파이썬으로 하위 작업 실행
_BACKUP_DEST = r"G:\내 드라이브\ASAB_backup"


def _py(*args: str) -> list[str]:
    return [PY, *args]


# (그룹명, [(라벨, [명령들], 설명, 위험여부), ...])
MENU: list[tuple[str, list[tuple[str, list[list[str]], str, bool]]]] = [
    ("장중 단타 (Track 2)", [
        ("단타 봇 시작 (실매매)", [_py("-m", "src.surge_bot")],
         "스캔→매수→익절/손절 자동 반복. Ctrl+C 로 중지", True),
        ("단타 봇 점검 (주문 없음)", [_py("-m", "src.surge_bot", "--dry-run")],
         "지금 무엇을 살지 dry-run 으로 확인", False),
        ("단타 봇 1회 테스트", [_py("-m", "src.surge_bot", "--once")],
         "실주문 1주기만 (체결/기록 확인)", True),
    ]),
    ("단타 데이터·학습", [
        ("분봉 수집 (장 마감 후)", [_py("-m", "src.collect_minute")],
         "그날 스캔 종목의 당일 분봉 저장(같은 날 15:30 이후)", False),
        ("단타 학습", [_py("-m", "src.surge_ml", "--train")],
         "쌓인 데이터로 모델 학습(데이터 부족시 거부)", False),
        ("청산 분석", [_py("-m", "src.surge_exits")],
         "분봉으로 익절/손절 조합별 순수익 비교", False),
    ]),
    ("일봉 로테이션 (Track 1)", [
        ("일봉 봇 시작", [_py("-m", "src.run_bot")],
         "검증된 추세 로테이션 자동매매", True),
        ("일봉 봇 사전점검", [_py("-m", "src.run_bot", "--once", "--dry-run")],
         "주문 없이 타깃/잔고만 확인", False),
    ]),
    ("데이터 수집·관리", [
        ("일봉 업데이트", [_py("-m", "src.collect", "--source", "master", "--update")],
         "최신 일봉 갱신", False),
        ("수집 이어하기", [_py("-m", "src.universe", "--liquidity"),
                       _py("-m", "src.collect", "--source", "master",
                           "--months", "120", "--skip-existing")],
         "시총조사 + 일봉 백필 이어서", False),
        ("수급 백필", [_py("-m", "src.collect_analytics", "--months", "60", "--limit", "300")],
         "투자자·신용 등 수급 데이터 수집", False),
    ]),
    ("도구", [
        ("대시보드 켜기", [_py("-m", "streamlit", "run", "dashboard.py",
                          "--server.port", "8501")],
         "브라우저 대시보드(Ctrl+C 로 종료)", False),
        ("백업 시작", [_py("-m", "src.backup", "--dest", _BACKUP_DEST, "--watch",
                       "--threshold", "100000", "--interval", "300")],
         "구글드라이브로 자동 백업 감시", False),
        ("설치 (의존성)", [_py("-m", "pip", "install", "-r", "requirements.txt")],
         "필요 패키지 설치/업데이트", False),
    ]),
]


def _flat() -> list[tuple[str, list[list[str]], str, bool]]:
    return [item for _, items in MENU for item in items]


def _print_menu() -> None:
    print("\n" + "=" * 56)
    print("  ASAB 통합 실행 메뉴")
    print("=" * 56)
    n = 0
    for group, items in MENU:
        print(f"\n[{group}]")
        for label, _cmds, desc, danger in items:
            n += 1
            mark = " ⚠️" if danger else ""
            print(f"  {n:>2}. {label}{mark}")
            print(f"      └ {desc}")
    print("\n   0. 종료")
    print("-" * 56)


def _run(cmds: list[list[str]], label: str) -> None:
    print(f"\n▶ 실행: {label}\n" + "-" * 56)
    try:
        for c in cmds:
            rc = subprocess.call(c)
            if rc != 0:
                print(f"\n(종료코드 {rc})")
                break
    except KeyboardInterrupt:
        print("\n중지됨(Ctrl+C).")
    print("-" * 56)
    input("\n엔터를 누르면 메뉴로 돌아갑니다... ")


def main() -> None:
    items = _flat()
    while True:
        _print_menu()
        try:
            choice = input("번호 선택> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice in ("0", "q", "Q"):
            return
        if not choice.isdigit() or not (1 <= int(choice) <= len(items)):
            print("⚠️ 올바른 번호를 입력하세요.")
            continue
        label, cmds, _desc, danger = items[int(choice) - 1]
        if danger:
            ok = input(f"'{label}' 은 실제(모의) 주문을 냅니다. 진행? (y/N) ").strip().lower()
            if ok != "y":
                print("취소됨.")
                continue
        _run(cmds, label)


if __name__ == "__main__":
    main()
