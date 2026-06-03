"""OAuth 접근토큰 발급/캐싱.

KIS 토큰은 발급 후 24시간(86400초) 유효하며, 1분에 1회만 발급 가능하다.
따라서 파일에 캐싱하여 만료 전까지 재사용한다.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from .config import Config

_TOKEN_DIR = Path(".token_cache")


class TokenManager:
    def __init__(self, config: Config):
        self.config = config
        _TOKEN_DIR.mkdir(exist_ok=True)
        mode = "paper" if config.paper_trading else "real"
        # 앱키 일부를 파일명에 넣어 계정별로 분리
        self._cache_file = _TOKEN_DIR / f"token_{mode}_{config.app_key[:8]}.json"
        self._token: str | None = None
        self._expire_at: float = 0.0
        self._load_cache()

    def _load_cache(self) -> None:
        if not self._cache_file.exists():
            return
        try:
            data = json.loads(self._cache_file.read_text(encoding="utf-8"))
            self._token = data.get("access_token")
            self._expire_at = float(data.get("expire_at", 0))
        except (json.JSONDecodeError, ValueError):
            self._token = None
            self._expire_at = 0.0

    def _save_cache(self) -> None:
        self._cache_file.write_text(
            json.dumps({"access_token": self._token, "expire_at": self._expire_at}),
            encoding="utf-8",
        )

    def _is_valid(self) -> bool:
        # 만료 5분 전이면 갱신
        return bool(self._token) and time.time() < (self._expire_at - 300)

    def get_token(self) -> str:
        if self._is_valid():
            return self._token  # type: ignore[return-value]
        return self._issue()

    def _issue(self) -> str:
        url = f"{self.config.base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
        }
        resp = requests.post(url, json=body, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if "access_token" not in data:
            raise RuntimeError(f"토큰 발급 실패: {data}")

        self._token = data["access_token"]
        # expires_in(초) 또는 access_token_token_expired(문자열) 제공됨
        expires_in = int(data.get("expires_in", 86400))
        self._expire_at = time.time() + expires_in
        self._save_cache()
        when = datetime.now() + timedelta(seconds=expires_in)
        print(f"[auth] 새 토큰 발급 (만료 예정: {when:%Y-%m-%d %H:%M})")
        return self._token
