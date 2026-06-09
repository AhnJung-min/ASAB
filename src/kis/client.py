"""KIS REST 공통 클라이언트. 헤더 구성, hashkey 발급, 요청 래핑.

모의투자 서버는 초당 호출 제한이 엄격하므로(EGW00201) 요청 간 최소 간격을
두고, 한도 초과 응답이나 네트워크 끊김 시 잠시 대기 후 재시도한다.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import requests

from .auth import TokenManager
from .config import Config

# 초당 거래건수 초과 에러코드
_RATE_LIMIT_CODE = "EGW00201"
# 토큰 무효/만료 에러코드(서버가 토큰 거부) → 재발급 후 재시도
_TOKEN_ERR_CODES = {"EGW00121", "EGW00123", "EGW00105"}
# 요청 간 최소 간격(초). 모의투자 서버는 원장(잔고·주문) 초당 한도(EGW00201)가
# 특히 빡빡해 0.8초(≈초당 1.25회)로 보수적 — 주문·잔고가 몰려도 한도 아래로 억제.
_MIN_INTERVAL = 0.8
# 한도 초과 시 최대 재시도 횟수. 잠깐의 초과를 조용히 흡수하도록 넉넉히(6회).
_MAX_RETRY = 6


class KISClient:
    def __init__(self, config: Config):
        self.config = config
        self.tokens = TokenManager(config)
        self._session = requests.Session()
        self._lock = threading.Lock()
        self._last_call = 0.0

    def _throttle(self) -> None:
        """직전 요청과 최소 간격을 보장한다(스레드 안전)."""
        with self._lock:
            elapsed = time.monotonic() - self._last_call
            if elapsed < _MIN_INTERVAL:
                time.sleep(_MIN_INTERVAL - elapsed)
            self._last_call = time.monotonic()

    # --- 헤더 ---------------------------------------------------------------
    def _headers(self, tr_id: str, *, hashkey: str | None = None) -> dict[str, str]:
        h = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.tokens.get_token()}",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
            "tr_id": tr_id,
            "custtype": "P",  # 개인
        }
        if hashkey:
            h["hashkey"] = hashkey
        return h

    def _hashkey(self, body: dict[str, Any]) -> str:
        """주문 등 POST body 위변조 방지용 hashkey 발급."""
        url = f"{self.config.base_url}/uapi/hashkey"
        headers = {
            "content-type": "application/json; charset=utf-8",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
        }
        resp = self._session.post(url, json=body, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()["HASH"]

    # --- 요청 ---------------------------------------------------------------
    def get(self, path: str, tr_id: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.config.base_url}{path}"
        return self._request_with_retry(
            lambda: self._session.get(
                url, headers=self._headers(tr_id), params=params, timeout=10
            )
        )

    def post(self, path: str, tr_id: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.config.base_url}{path}"
        return self._request_with_retry(
            lambda: self._session.post(
                url,
                headers=self._headers(tr_id, hashkey=self._hashkey(body)),
                json=body,
                timeout=10,
            )
        )

    def _request_with_retry(self, do_request) -> dict[str, Any]:
        last_err: Exception | None = None
        for attempt in range(_MAX_RETRY):
            self._throttle()
            try:
                return self._parse(do_request())
            except KISApiError as e:
                if e.code in _TOKEN_ERR_CODES:
                    # 토큰 거부 → 무효화 후 재시도(다음 do_request 가 새 토큰으로 헤더 구성)
                    self.tokens.invalidate()
                    last_err = e
                    continue
                if e.code != _RATE_LIMIT_CODE:
                    raise
                last_err = e
                # 초당 한도는 1~2초면 풀리므로 백오프 상한 2초(6회 재시도 흡수용)
                time.sleep(min(0.5 * (2 ** attempt), 2.0))
            except requests.exceptions.RequestException as e:
                # 네트워크 끊김(RemoteDisconnected)·타임아웃 등도 재시도.
                # (안 하면 백필 중 끊겨 과거 데이터가 잘린 채 남는 버그)
                last_err = e
                time.sleep(0.5 * (2 ** attempt))
        assert last_err is not None
        raise last_err

    @staticmethod
    def _parse(resp: requests.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise RuntimeError(f"JSON 아님: {resp.text[:200]}")
        # rt_cd: '0' 성공, 그 외 실패
        if data.get("rt_cd") not in (None, "0"):
            raise KISApiError(data.get("msg_cd", ""), data.get("msg1", str(data)))
        return data


class KISApiError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")
