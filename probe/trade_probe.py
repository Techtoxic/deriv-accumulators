#!/usr/bin/env python3
"""Validate full ACCU trade lifecycle on demo: buy -> monitor -> sell."""
import os, json, asyncio, time, urllib.request, ssl
import websockets

def load_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line=line.strip()
            if line and not line.startswith("#") and "=" in line:
                k,v=line.split("=",1); env[k.strip()]=v.strip()
    return env
E=load_env(os.path.join(os.path.dirname(__file__),"..",".env"))
TOKEN=E["DERIV_TOKEN"]; APP_ID=E["DERIV_APP_ID"]; BASE=E["DERIV_REST_BASE"]

def rest(method,path,body=None):
    req=urllib.request.Request(BASE+path,data=(json.dumps(body).encode() if body else None),method=method)
    req.add_header("Authorization",f"Bearer {TOKEN}"); req.add_header("Deriv-App-ID",APP_ID)
    req.add_header("Content-Type","application/json")
    try:
        with urllib.request.urlopen(req,context=ssl.create_default_context(),timeout=30) as r:
            raw=r.read().decode()
    except urllib.error.HTTPError as e:
        raw=e.read().decode()
    try: return json.loads(raw)
    except: return {"_raw":raw[:200]}

async def call(ws,payload,want,timeout=20):
    await ws.send(json.dumps(payload))
    end=time.time()+timeout
    while time.time()<end:
        m=json.loads(await asyncio.wait_for(ws.recv(),timeout=timeout))
        if m.get("msg_type")==want or "error" in m: return m
    return {"error":{"message":"timeout "+want}}

async def main():
    accts=rest("GET","/trading/v1/options/accounts").get("data",[])
    demo=next(a for a in accts if a["account_type"]=="demo")
    print("demo balance before:",demo["balance"])
    url=rest("POST",f"/trading/v1/options/accounts/{demo['account_id']}/otp")["data"]["url"]
    async with websockets.connect(url,max_size=4*1024*1024) as ws:
        print("\n-- BUY $1 ACCU 1% on 1HZ50V --")
        buy=await call(ws,{"buy":"1","price":100,"parameters":{
            "amount":1,"basis":"stake","contract_type":"ACCU","currency":"USD",
            "underlying_symbol":"1HZ50V","growth_rate":0.01}},"buy")
        if "error" in buy:
            print("BUY ERROR:",json.dumps(buy["error"])); return
        b=buy["buy"]; cid=b["contract_id"]
        print(f"bought contract_id={cid} buy_price={b['buy_price']} payout={b['payout']} bal_after={b['balance_after']}")

        print("\n-- MONITOR via proposal_open_contract (subscribe) --")
        await ws.send(json.dumps({"proposal_open_contract":1,"contract_id":cid,"subscribe":1,"req_id":50}))
        seen=0; last=None
        end=time.time()+25
        while time.time()<end and seen<8:
            m=json.loads(await asyncio.wait_for(ws.recv(),timeout=20))
            if m.get("msg_type")=="proposal_open_contract":
                c=m["proposal_open_contract"]; last=c
                print(f"  tick_passed={c.get('tick_passed')} spot={c.get('current_spot')} "
                      f"hi={c.get('high_barrier')} lo={c.get('low_barrier')} bid={c.get('bid_price')} "
                      f"profit={c.get('profit')} valid_sell={c.get('is_valid_to_sell')} status={c.get('status')}")
                seen+=1

        print("\n-- SELL at market --")
        if last and last.get("is_sold"):
            print("already sold/expired, status:",last.get("status"))
        else:
            sell=await call(ws,{"sell":cid,"price":0,"req_id":60},"sell")
            print("sell resp:",json.dumps(sell.get("sell",sell))[:300])
        # final state
        fin=await call(ws,{"proposal_open_contract":1,"contract_id":cid,"req_id":70},"proposal_open_contract")
        c=fin.get("proposal_open_contract",{})
        print(f"\nFINAL: status={c.get('status')} is_sold={c.get('is_sold')} profit={c.get('profit')} "
              f"buy_price={c.get('buy_price')} sell_price={c.get('sell_price')} tick_passed={c.get('tick_passed')}")
    accts=rest("GET","/trading/v1/options/accounts").get("data",[])
    demo=next(a for a in accts if a["account_type"]=="demo")
    print("demo balance after:",demo["balance"])

asyncio.run(main())
