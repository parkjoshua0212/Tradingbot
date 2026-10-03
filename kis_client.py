"""KIS Open API client, MOCK environment only.
tr_ids below are the mock (VTS) ones; verify against apiportal.koreainvestment.com if KIS changes them."""
import json
import logging
import os
import threading
import time

import requests

import data  # noqa: F401  (loads .env on import)

log = logging.getLogger(__name__)

MOCK_URL = "https://openapivts.koreainvestment.com:29443"
TOKEN_FILE = ".kis_token.json"  # gitignored
TOKEN_MARGIN = 600  # reissue when <10 min left

TR = {
    "price": "FHKST01010100",
    "balance": "VTTC8434R",
    "buy": "VTTC0012U",
    "sell": "VTTC0011U",
    "orders": "VTTC0081R",
    "psbl": "VTTC8908R",
}


class KISError(Exception):
    def __init__(self, msg_cd: str, msg: str):
        super().__init__(f"{msg_cd}: {msg}")
        self.msg_cd = msg_cd


class RateLimiter:
    """Blocks so calls are at least min_interval seconds apart. clock/sleep injectable for tests."""

    def __init__(self, min_interval: float, clock=time.monotonic, sleep=time.sleep):
        self.min_interval, self.clock, self.sleep = min_interval, clock, sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self.clock()
            if now < self._next:
                self.sleep(self._next - now)
                now = self._next
            self._next = now + self.min_interval


class KISClient:
    def __init__(self, app_key=None, app_secret=None, account=None, product_code=None,
                 base_url=MOCK_URL, min_interval=0.6, session=None, token_file=TOKEN_FILE,
                 clock=time.time, limiter=None, sleep=time.sleep):
        if base_url.rstrip("/") != MOCK_URL:
            raise ValueError(f"refusing to start: base_url must be the mock URL {MOCK_URL}, got {base_url}")
        env = os.environ
        self.key = app_key or env["KIS_MOCK_APP_KEY"]
        self.secret = app_secret or env["KIS_MOCK_APP_SECRET"]
        self.account = account or env["KIS_MOCK_ACCOUNT"]
        self.product = product_code or env["KIS_MOCK_PRODUCT_CODE"]
        self.base_url = MOCK_URL
        self.http = session or requests.Session()
        self.token_file, self.clock, self.sleep = token_file, clock, sleep
        self.limiter = limiter or RateLimiter(min_interval)
        self._token = None

    # --- auth ---
    def token(self) -> str:
        now = self.clock()
        if self._token and self._token["expires_at"] - TOKEN_MARGIN > now and self._token["key"] == self.key:
            return self._token["token"]
        try:
            with open(self.token_file, encoding="utf-8") as f:
                cached = json.load(f)
            if cached["key"] == self.key and cached["expires_at"] - TOKEN_MARGIN > now:
                self._token = cached
                return cached["token"]
        except (FileNotFoundError, KeyError, ValueError):
            pass
        # No retry: reissue is rate-limited, a failure should surface.
        self.limiter.wait()
        r = self.http.post(f"{self.base_url}/oauth2/tokenP", timeout=10, json={
            "grant_type": "client_credentials", "appkey": self.key, "appsecret": self.secret})
        body = r.json() if r.content else {}
        if r.status_code != 200 or "access_token" not in body:
            raise KISError(body.get("error_code", str(r.status_code)), body.get("error_description", r.text[:200]))
        self._token = {"token": body["access_token"], "key": self.key,
                       "expires_at": now + int(body.get("expires_in", 86400))}
        with open(self.token_file, "w", encoding="utf-8") as f:
            json.dump(self._token, f)
        return self._token["token"]

    # --- transport ---
    def _call(self, method, path, tr_id, *, params=None, body=None, retries=3):
        """Retries (backoff) only for GET. A POST (order) is sent exactly once."""
        url = f"{self.base_url}{path}"
        headers = {"authorization": f"Bearer {self.token()}", "appkey": self.key, "appsecret": self.secret,
                   "tr_id": tr_id, "custtype": "P", "content-type": "application/json; charset=utf-8"}
        tries = retries if method == "GET" else 1
        for attempt in range(tries):
            self.limiter.wait()
            last = attempt == tries - 1
            try:
                r = self.http.request(method, url, headers=headers, params=params, json=body, timeout=10)
            except requests.RequestException as e:
                if last:
                    raise
                log.warning("KIS %s failed (%s), retrying", path, e)
            else:
                if (r.status_code < 500 and r.status_code != 429) or last:
                    break
                log.warning("KIS %s -> HTTP %s, retrying", path, r.status_code)
            self.sleep(2 ** attempt)
        out = r.json() if r.content else {}
        if r.status_code != 200 or out.get("rt_cd", "0") != "0":
            raise KISError(out.get("msg_cd", str(r.status_code)), out.get("msg1", r.text[:200]))
        return out

    # --- API ---
    def get_price(self, ticker: str) -> int:
        d = self._call("GET", "/uapi/domestic-stock/v1/quotations/inquire-price", TR["price"],
                       params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker})
        return int(d["output"]["stck_prpr"])

    def _balance_raw(self) -> dict:
        return self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-balance", TR["balance"], params={
            "CANO": self.account, "ACNT_PRDT_CD": self.product, "AFHR_FLPR_YN": "N", "OFL_YN": "",
            "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "00", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})

    def get_balance(self) -> dict:
        """{'cash': deposit, 'orderable': buyable cash, 'equity', 'positions': {ticker: {'qty','avg_price','name'}}}.
        Size orders on 'orderable' (inquire-psbl-order), not 'cash' (dnca_tot_amt)."""
        d = self._balance_raw()
        pos = {p["pdno"]: {"qty": int(p["hldg_qty"]), "avg_price": float(p["pchs_avg_pric"]), "name": p["prdt_name"]}
               for p in d["output1"] if int(p["hldg_qty"]) > 0}
        s = d["output2"][0]
        return {"cash": int(s["dnca_tot_amt"]), "orderable": self.get_orderable_cash(), "equity": int(s["tot_evlu_amt"]),
                "positions": pos}

    def get_orderable_cash(self, probe: str = "005930") -> int:
        """ord_psbl_cash (cash, no margin/credit) from inquire-psbl-order for a market order; not ticker-specific."""
        d = self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-psbl-order", TR["psbl"], params={
            "CANO": self.account, "ACNT_PRDT_CD": self.product, "PDNO": probe, "ORD_UNPR": "0", "ORD_DVSN": "01",
            "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"})
        return int(d["output"]["ord_psbl_cash"])

    def place_order(self, ticker: str, side: str, qty: int, order_type: str = "market", price: int = 0) -> str:
        """Returns the order id (ODNO). Raises KISError on rejection. Never retried."""
        if side not in ("buy", "sell") or order_type not in ("market", "limit"):
            raise ValueError(f"bad side/order_type: {side}/{order_type}")
        if not isinstance(qty, int) or qty <= 0:
            raise ValueError(f"qty must be a positive int, got {qty!r}")
        if order_type == "limit" and price <= 0:
            raise ValueError("limit order needs a price")
        d = self._call("POST", "/uapi/domestic-stock/v1/trading/order-cash", TR[side], body={
            "CANO": self.account, "ACNT_PRDT_CD": self.product, "PDNO": ticker,
            "ORD_DVSN": "01" if order_type == "market" else "00",
            "ORD_QTY": str(qty), "ORD_UNPR": "0" if order_type == "market" else str(price),
            "EXCG_ID_DVSN_CD": "KRX", "SLL_TYPE": "01" if side == "sell" else "", "CNDT_PRIC": ""})
        return d["output"]["ODNO"]

    def get_order_status(self, order_id: str, day: str) -> dict | None:
        """day = YYYYMMDD of the order. {'order_id','qty','filled','avg_price','open'} or None if not found."""
        d = self._call("GET", "/uapi/domestic-stock/v1/trading/inquire-daily-ccld", TR["orders"], params={
            "CANO": self.account, "ACNT_PRDT_CD": self.product, "INQR_STRT_DT": day, "INQR_END_DT": day,
            "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", "PDNO": "", "CCLD_DVSN": "00", "ORD_GNO_BRNO": "",
            "ODNO": order_id, "INQR_DVSN_3": "00", "INQR_DVSN_1": "", "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""})
        for o in d["output1"]:
            if o["odno"] == order_id:
                filled, qty = int(o["tot_ccld_qty"]), int(o["ord_qty"])
                return {"order_id": order_id, "qty": qty, "filled": filled,
                        "avg_price": float(o.get("avg_prvs") or 0), "open": filled < qty and o.get("cncl_yn") != "Y"}
        return None
