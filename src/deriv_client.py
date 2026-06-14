"""
Async client for Deriv's NEW API platform (PAT app).

Flow (verified live):
  REST  GET  /trading/v1/options/accounts            -> pick demo account_id
  REST  POST /trading/v1/options/accounts/{id}/otp    -> authenticated WS url
  WS    wss://.../trading/v1/options/ws/demo?otp=...   -> trade with same
        message protocol as the classic API (proposal/buy/sell/ticks/...).

Provides request/response correlation by req_id and lightweight subscription
routing (ticks, proposal_open_contract), plus automatic reconnect.
"""
from __future__ import annotations
import asyncio
import json
import ssl
import time
import urllib.request
import urllib.error
from typing import Any, Dict, Optional, Callable

import websockets

from config import Config


class DerivError(Exception):
    def __init__(self, code: str, message: str, payload: dict = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.payload = payload or {}


class DerivClient:
    def __init__(self, cfg: Config, log: Callable[[str], None] = print):
        self.cfg = cfg
        self.log = log
        self.account_id: Optional[str] = None
        self.account_balance: Optional[float] = None
        self._ws = None
        self._req_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._tick_queues: Dict[str, asyncio.Queue] = {}
        self._poc_queues: Dict[int, asyncio.Queue] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._connected = asyncio.Event()
        self._closing = False
        self._ssl = ssl.create_default_context()

    # ------------------------------------------------------------------ REST
    def _rest(self, method: str, path: str, body: dict = None) -> Any:
        url = self.cfg.conn.rest_base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.cfg.conn.token}")
        req.add_header("Deriv-App-ID", self.cfg.conn.app_id)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, context=self._ssl, timeout=30) as r:
                raw = r.read().decode()
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            try:
                return json.loads(raw)
            except Exception:
                raise DerivError("HTTP_" + str(e.code), raw[:200])
        try:
            return json.loads(raw)
        except Exception:
            return {"_raw": raw}

    def resolve_account(self) -> dict:
        """Pick the demo account (hard safety: never the real account)."""
        resp = self._rest("GET", "/trading/v1/options/accounts")
        accts = resp.get("data", []) if isinstance(resp, dict) else []
        want = self.cfg.conn.account_type
        demo = next((a for a in accts if a.get("account_type") == want), None)
        if not demo:
            raise DerivError("NO_ACCOUNT", f"No {want} account found in {accts}")
        self.account_id = demo["account_id"]
        try:
            self.account_balance = float(demo.get("balance", 0))
        except Exception:
            self.account_balance = None
        return demo

    def _get_ws_url(self) -> str:
        resp = self._rest("POST", f"/trading/v1/options/accounts/{self.account_id}/otp")
        url = (resp or {}).get("data", {}).get("url")
        if not url:
            raise DerivError("NO_OTP", f"OTP response missing url: {resp}")
        return url

    def reset_demo_balance(self) -> Any:
        if self.cfg.conn.account_type != "demo":
            raise DerivError("NOT_DEMO", "refusing to reset non-demo balance")
        return self._rest("POST", f"/trading/v1/options/accounts/{self.account_id}/reset-demo-balance")

    # ------------------------------------------------------------------- WS
    async def connect(self):
        if not self.account_id:
            self.resolve_account()
        url = self._get_ws_url()
        self._ws = await websockets.connect(url, max_size=self.cfg.conn.ws_max_size,
                                             ping_interval=20, ping_timeout=20)
        self._connected.set()
        self._reader_task = asyncio.create_task(self._reader())
        self.log(f"WS connected ({self.cfg.conn.account_type} {self.account_id})")

    async def _reader(self):
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                self._dispatch(msg)
        except Exception as e:
            self._connected.clear()
            if not self._closing:
                self.log(f"WS reader stopped: {e!r}; reconnecting")
                asyncio.create_task(self._reconnect())

    def _dispatch(self, msg: dict):
        mtype = msg.get("msg_type")
        # 1) route streaming messages
        if mtype == "tick" and "tick" in msg:
            sym = msg["tick"].get("symbol")
            q = self._tick_queues.get(sym)
            if q:
                q.put_nowait(msg["tick"])
            return
        if mtype == "proposal_open_contract":
            poc = msg.get("proposal_open_contract", {}) or {}
            cid = poc.get("contract_id")
            q = self._poc_queues.get(cid)
            if q:
                q.put_nowait(poc)
            # also resolve a pending one-shot request if present
        # 2) resolve request/response futures by req_id
        rid = msg.get("req_id")
        if rid is not None and rid in self._pending:
            fut = self._pending.pop(rid)
            if not fut.done():
                fut.set_result(msg)

    async def _reconnect(self):
        delay = self.cfg.conn.reconnect_min
        while not self._closing:
            try:
                await asyncio.sleep(delay)
                self.resolve_account()
                url = self._get_ws_url()
                self._ws = await websockets.connect(url, max_size=self.cfg.conn.ws_max_size,
                                                     ping_interval=20, ping_timeout=20)
                self._connected.set()
                self._reader_task = asyncio.create_task(self._reader())
                self.log("WS reconnected")
                # re-subscribe ticks
                for sym in list(self._tick_queues.keys()):
                    await self._send({"ticks": sym, "subscribe": 1})
                return
            except Exception as e:
                self.log(f"reconnect failed: {e!r}")
                delay = min(delay * 2, self.cfg.conn.reconnect_max)

    async def _send(self, payload: dict):
        await self._ws.send(json.dumps(payload))

    async def request(self, payload: dict, timeout: float = None) -> dict:
        timeout = timeout or self.cfg.conn.request_timeout
        self._req_id += 1
        rid = self._req_id
        payload = dict(payload, req_id=rid)
        fut = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self._send(payload)
        try:
            msg = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise DerivError("TIMEOUT", f"no response for {list(payload)[0]}")
        if "error" in msg and msg["error"]:
            err = msg["error"]
            raise DerivError(err.get("code", "ERR"), err.get("message", ""), msg)
        return msg

    # ------------------------------------------------------ high-level calls
    async def subscribe_ticks(self, symbol: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._tick_queues[symbol] = q
        await self._send({"ticks": symbol, "subscribe": 1})
        return q

    async def ticks_history(self, symbol: str, count: int = 5000, end="latest") -> dict:
        msg = await self.request({"ticks_history": symbol, "count": count,
                                  "end": end, "style": "ticks"})
        return msg.get("history", {})

    async def balance(self) -> dict:
        msg = await self.request({"balance": 1})
        return msg.get("balance", {})

    async def proposal_accu(self, growth_rate: float, symbol: str = None,
                            amount: float = None) -> dict:
        s = self.cfg.strat
        msg = await self.request({
            "proposal": 1,
            "amount": amount if amount is not None else s.stake,
            "basis": "stake",
            "contract_type": "ACCU",
            "currency": s.currency,
            "underlying_symbol": symbol or s.symbol,
            "growth_rate": growth_rate,
        })
        return msg.get("proposal", {})

    async def buy_accu(self, growth_rate: float, symbol: str = None,
                       amount: float = None, take_profit: float = None) -> dict:
        s = self.cfg.strat
        params = {
            "amount": amount if amount is not None else s.stake,
            "basis": "stake",
            "contract_type": "ACCU",
            "currency": s.currency,
            "underlying_symbol": symbol or s.symbol,
            "growth_rate": growth_rate,
        }
        if take_profit and take_profit > 0:
            params["limit_order"] = {"take_profit": take_profit}
        # price = max acceptable buy price; stake-based ACCU buy_price == stake
        msg = await self.request({"buy": "1", "price": params["amount"], "parameters": params})
        return msg.get("buy", {})

    async def subscribe_contract(self, contract_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._poc_queues[contract_id] = q
        await self._send({"proposal_open_contract": 1, "contract_id": contract_id, "subscribe": 1})
        return q

    async def contract_state(self, contract_id: int) -> dict:
        msg = await self.request({"proposal_open_contract": 1, "contract_id": contract_id})
        return msg.get("proposal_open_contract", {})

    async def sell(self, contract_id: int, price: float = 0) -> dict:
        msg = await self.request({"sell": contract_id, "price": price})
        return msg.get("sell", {})

    async def forget_contract(self, contract_id: int):
        self._poc_queues.pop(contract_id, None)

    async def close(self):
        self._closing = True
        try:
            if self._ws:
                await self._ws.close()
        except Exception:
            pass
