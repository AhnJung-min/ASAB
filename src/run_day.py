"""하루 자동 운행 — 무인(無人) 모드.

아침에 켜놓고 나가면:
  1) 09:00 까지 대기 (이미 장중이면 즉시 시작)
  2) 단타 봇 실행 → 15:30 도달 시 자동 종료 (surge_bot --until 15:30)
  3) 마감 후 잠시 대기 → 당일 분봉 수집 + 일봉 결손 백필 (collect_minute)
  4) 구글드라이브 폴더로 DB 1회 백업 (backup --once)
  5) 18:00 노트북 자동 종료 예약 (구글드라이브 업로드 시간 확보,
     --no-shutdown 으로 끌 수 있음 / 예약 취소는  shutdown /a)

실행:  py -m src.run_day   (ASAB.bat 메뉴에서도 선택 가능)
주말이면 아무것도 하지 않고 종료한다. 마감 후 보유 종목은 그대로 이월된다.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

OPEN_T = "09:00"
CLOSE_T = "15:30"
POST_CLOSE_WAIT_S = 300  # 마감 직후 분봉 데이터 안정화 대기(5분)
BACKUP_DEST = r"G:\내 드라이브\ASAB_backup"  # 구글드라이브 동기화 폴더(menu.py와 동일)
SHUTDOWN_T = "18:00"  # 자동 종료 시각(백업 파일 클라우드 업로드 여유 확보)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _run(label: str, *mod_args: str) -> int:
    log(f"▶ {label}")
    rc = subprocess.call([sys.executable, "-m", *mod_args])
    log(f"  └ {label} 종료 (코드 {rc})")
    return rc


def _schedule_shutdown(at: str) -> None:
    """오늘 `at`(HH:MM)에 종료되도록 Windows에 예약한다. 이미 지났으면 2분 뒤."""
    now = datetime.now()
    h, m = map(int, at.split(":"))
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    secs = max(int((target - now).total_seconds()), 120)
    # /f: 무인 상태에서 다른 앱이 종료를 막지 않도록 강제(저장 안 한 작업은 날아감)
    rc = subprocess.call(["shutdown", "/s", "/f", "/t", str(secs)])
    if rc == 0:
        log(f"🔌 노트북 종료 예약 완료 → {target:%H:%M} (취소하려면: shutdown /a)")
    else:
        log(f"⚠️ 종료 예약 실패(코드 {rc}) — 이미 예약돼 있으면 shutdown /a 후 재시도")


def main() -> None:
    ap = argparse.ArgumentParser(description="하루 자동 운행(봇→분봉수집→백업→종료예약)")
    ap.add_argument("--no-shutdown", action="store_true",
                    help="마지막 노트북 자동 종료 예약을 생략")
    args = ap.parse_args()

    now = datetime.now()
    if now.weekday() >= 5:
        log("오늘은 주말 — 장이 없으므로 종료합니다.")
        return

    # 1) 개장 대기
    if now.strftime("%H:%M") < OPEN_T:
        open_dt = now.replace(hour=9, minute=0, second=0, microsecond=0)
        wait = (open_dt - now).total_seconds()
        log(f"개장(09:00)까지 {wait / 60:.0f}분 대기합니다...")
        time.sleep(wait)

    # 2) 장중 단타 봇 (15:30 자동 종료)
    if datetime.now().strftime("%H:%M") < CLOSE_T:
        _run(f"단타 봇 (— {CLOSE_T} 자동 종료)", "src.surge_bot", "--until", CLOSE_T)
    else:
        log("이미 장 마감 이후 — 봇 단계는 건너뜁니다.")

    # 3) 마감 후 분봉 수집
    log(f"분봉 데이터 안정화를 위해 {POST_CLOSE_WAIT_S // 60}분 대기...")
    time.sleep(POST_CLOSE_WAIT_S)
    _run("당일 분봉 수집 (+일봉 결손 백필)", "src.collect_minute")

    # 4) 구글드라이브 백업 (1회 스냅샷)
    _run("DB 백업 → 구글드라이브", "src.backup", "--dest", BACKUP_DEST, "--once")

    # 5) 노트북 자동 종료 예약
    if args.no_shutdown:
        log("종료 예약 생략(--no-shutdown).")
    else:
        _schedule_shutdown(SHUTDOWN_T)

    log("✅ 하루 운행 완료.")


if __name__ == "__main__":
    main()
