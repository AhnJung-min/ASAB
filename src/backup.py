"""SQLite DB 자동 백업.

수집 중에도 안전한 '일관 스냅샷'(sqlite3 backup API)을 떠서 지정 폴더에 저장한다.
그냥 파일 복사와 달리 쓰기 도중에도 깨지지 않는다.

watch 모드: 일봉 데이터가 일정량(threshold) 늘어날 때마다 자동 백업(이벤트 기반).
백업 폴더를 구글드라이브 동기화 폴더로 지정하면 → 구글드라이브가 자동 업로드.

사용:
  python -m src.backup --dest "G:\\내 드라이브\\ASAB_backup" --once
  python -m src.backup --dest "..." --watch --threshold 5000 --interval 300
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from .data.store import DB_PATH


def _row_count(db: Path) -> int:
    """백업 트리거용 '활동량' = 일봉 + 단타 스캔 행수.

    일봉(daily_price)은 백필 때만 늘고, 단타(surge_scan)는 봇 돌 때 는다.
    둘을 합쳐야 어느 국면이든 데이터 증가를 감지한다.
    """
    con = sqlite3.connect(db, timeout=30)
    try:
        total = 0
        for tbl in ("daily_price", "surge_scan"):
            try:
                total += con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            except sqlite3.OperationalError:
                pass  # 테이블 없으면 0
        return total
    finally:
        con.close()


def snapshot(dest_dir: Path, keep: int = 3) -> Path:
    """안전한 일관 스냅샷을 떠서 dest_dir 에 저장. 최신본 + 타임스탬프 사본 유지."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    latest = dest_dir / "market_latest.db"
    tmp = dest_dir / "market_latest.db.tmp"

    src = sqlite3.connect(DB_PATH, timeout=60)
    dst = sqlite3.connect(tmp)
    try:
        with dst:
            src.backup(dst)  # 일관 스냅샷 (쓰기 중에도 안전)
    finally:
        dst.close()
        src.close()
    tmp.replace(latest)  # 원자적 교체

    # 타임스탬프 사본(최근 keep개만 유지)
    stamp = dest_dir / f"market_{datetime.now():%Y%m%d_%H%M%S}.db"
    shutil.copy2(latest, stamp)
    for old in sorted(dest_dir.glob("market_2*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return latest


def main() -> None:
    ap = argparse.ArgumentParser(description="수집 DB 자동 백업 (구글드라이브 등)")
    ap.add_argument("--dest", required=True, help="백업 폴더(클라우드 동기화 폴더 권장)")
    ap.add_argument("--once", action="store_true", help="1회 백업")
    ap.add_argument("--watch", action="store_true", help="데이터 증가 감시하며 자동 백업")
    ap.add_argument("--threshold", type=int, default=5000,
                    help="[watch] 이만큼 행이 늘면 백업 (기본 5000행 ≈ 종목 2개)")
    ap.add_argument("--interval", type=int, default=300, help="[watch] 감시 주기(초)")
    ap.add_argument("--max-age", type=int, default=1800,
                    help="[watch] 행 증가가 없어도 이 시간(초)마다 무조건 백업 (기본 30분). "
                         "0이면 비활성(행 증가시에만)")
    ap.add_argument("--keep", type=int, default=3, help="보관할 타임스탬프 사본 수")
    args = ap.parse_args()
    dest = Path(args.dest)

    def log(m: str) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)

    if args.watch:
        last = -1
        last_backup = 0.0
        age_txt = f"{args.max_age // 60}분" if args.max_age % 60 == 0 else f"{args.max_age}초"
        log(f"백업 감시 시작 → {dest}  (시작 시 즉시 1회 + 이후 행 "
            f"{args.threshold:,}개 증가 또는 {age_txt}마다)")
        while True:
            try:
                cur = _row_count(DB_PATH)
                aged = args.max_age > 0 and (time.time() - last_backup) >= args.max_age
                if last < 0 or cur - last >= args.threshold or aged:
                    p = snapshot(dest, args.keep)
                    sz = p.stat().st_size / 1e6
                    why = "시간" if (aged and cur - last < args.threshold and last >= 0) else "증가"
                    log(f"백업 완료({why}): 활동 {cur:,}행 ({sz:.0f}MB) → {p.name}")
                    last = cur
                    last_backup = time.time()
            except Exception as e:  # noqa: BLE001
                log(f"백업 오류: {e}")
            time.sleep(args.interval)
    else:
        p = snapshot(dest, args.keep)
        log(f"백업 완료 → {p}  ({p.stat().st_size/1e6:.0f}MB)")


if __name__ == "__main__":
    main()
