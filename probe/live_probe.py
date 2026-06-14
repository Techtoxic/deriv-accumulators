#!/usr/bin/env python3
"""Live probe of the NEW Deriv API (PAT app, demo). Verifies the full chain
needed by the accumulator bot: REST accounts -> OTP -> WS -> symbols/ticks/ACCU proposal."""
import os, json, asyncio, time, urllib.request, ssl
import websockets

def load_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env

E = load_env(os.path.join(os.path.dirname(__file__), "..", ".env"))
TOKEN = E["DERIV_TOKEN"]; APP_ID = E["DERIV_APP_ID"]; BASE = E["DERIV_REST_BASE"]

def rest(method, path, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Deriv-App-ID", APP_ID)
    req.add_header("Content-Type", "application/json")
    ctx = ssl.create_default_context()
    def parse(raw):
        try:
            return json.loads(raw)
        except Exception:
            return {"_raw": raw[:300]}
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            return r.status, parse(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, parse(e.read().decode() or "{}")

async def ws_call(ws, payload, want_type, timeout=20):
    await ws.send(json.dumps(payload))
    end = time.time() + timeout
    while time.time() < end:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
        if msg.get("msg_type") == want_type or "error" in msg:
            return msg
    return {"error": "timeout waiting for " + want_type}

async def main():
    print("== 1. HEALTH ==")
    print(rest("GET", "/v1/health"))

    print("\n== 2. LIST ACCOUNTS ==")
    st, accts = rest("GET", "/trading/v1/options/accounts")
    print("status", st)
    print(json.dumps(accts, indent=2)[:1500])
    data = accts.get("data", [])
    demo = next((a for a in data if a.get("account_type") == "demo"), data[0] if data else None)
    if not demo:
        print("NO ACCOUNT FOUND"); return
    acct_id = demo["account_id"]
    print("DEMO account_id:", acct_id, "balance:", demo.get("balance"), demo.get("currency"))

    print("\n== 3. OTP -> WS URL ==")
    st, otp = rest("POST", f"/trading/v1/options/accounts/{acct_id}/otp")
    print("status", st)
    print(json.dumps(otp, indent=2)[:600])
    ws_url = otp.get("data", {}).get("url")
    if not ws_url:
        print("NO WS URL"); return

    print("\n== 4. CONNECT WS ==")
    async with websockets.connect(ws_url, max_size=4*1024*1024) as ws:
        bal = await ws_call(ws, {"balance": 1, "req_id": 1}, "balance")
        print("balance:", json.dumps(bal.get("balance", bal))[:300])

        # find volatility symbols (esp 1-second ones for accumulators)
        sy = await ws_call(ws, {"active_symbols": "brief", "req_id": 2}, "active_symbols")
        syms = sy.get("active_symbols", [])
        print(f"\ntotal symbols: {len(syms)}")
        vols = [s for s in syms if "olatility" in s.get("display_name","") or s.get("symbol","").startswith(("R_","1HZ"))]
        for s in vols:
            print(f"  {s.get('symbol'):10} | {s.get('display_name'):28} | exchange_open={s.get('exchange_is_open')} | allow={s.get('allow_forward_starting')}")

        # ticks_history to verify cadence on 1HZ50V (Vol 50 1s)
        for sym in ["1HZ50V", "R_50"]:
            th = await ws_call(ws, {"ticks_history": sym, "count": 6, "end": "latest", "style":"ticks", "req_id": 3}, "history")
            h = th.get("history", {})
            times = h.get("times", [])
            if len(times) >= 2:
                deltas = [times[i+1]-times[i] for i in range(len(times)-1)]
                print(f"\n{sym} tick deltas(s): {deltas}  last prices: {h.get('prices',[])[-3:]}")
            else:
                print(f"\n{sym} history error: {json.dumps(th)[:200]}")

        # ACCU proposal on 1HZ50V at each growth rate
        print("\n== 5. ACCU PROPOSAL (1HZ50V) ==")
        for gr in [0.01, 0.02, 0.03, 0.04, 0.05]:
            p = await ws_call(ws, {
                "proposal": 1, "amount": 10, "basis": "stake",
                "contract_type": "ACCU", "currency": "USD",
                "underlying_symbol": "1HZ50V", "growth_rate": gr, "req_id": 10
            }, "proposal")
            if "error" in p:
                print(f"  gr={gr}: ERROR {json.dumps(p['error'])[:200]}"); continue
            pr = p["proposal"]; cd = pr.get("contract_details", {})
            print(f"  gr={gr}: spot={pr.get('spot')} payout={pr.get('payout')} "
                  f"bsd={cd.get('barrier_spot_distance')} tsb={cd.get('tick_size_barrier')} "
                  f"tsb%={cd.get('tick_size_barrier_percentage')} max_ticks={cd.get('maximum_ticks')} "
                  f"hi={cd.get('high_barrier')} lo={cd.get('low_barrier')}")
        # full dump of one proposal contract_details
        p = await ws_call(ws, {"proposal":1,"amount":10,"basis":"stake","contract_type":"ACCU",
            "currency":"USD","underlying_symbol":"1HZ50V","growth_rate":0.01,"req_id":11}, "proposal")
        print("\nFULL contract_details @1%:")
        print(json.dumps(p.get("proposal",{}).get("contract_details",{}), indent=2))

asyncio.run(main())
