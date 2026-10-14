import sys, sqlite3, numpy as np, pandas as pd
from dataclasses import replace
sys.path.insert(0,'/home/claude/trading-ai/trading_system')
from bot_v1 import condor as C
from bot_v1.config import CondorParams
from bot_v1.report import condor_charges_points
DB=sys.argv[1]
con=sqlite3.connect(f'file:{DB}?immutable=1',uri=True)
KEYS={"NIFTY":"NSE_INDEX|Nifty 50","SENSEX":"BSE_INDEX|SENSEX","BANKNIFTY":"NSE_INDEX|Nifty Bank",
      "FINNIFTY":"NSE_INDEX|Nifty Fin Service","MIDCPNIFTY":"NSE_INDEX|NIFTY MID SELECT","BANKEX":"BSE_INDEX|BANKEX"}
VIX="NSE_INDEX|India VIX"
P100={"NIFTY":100,"FINNIFTY":100,"MIDCPNIFTY":100,"SENSEX":300,"BANKNIFTY":300,"BANKEX":300}
PROTO=lambda u,p:0.25+0.003*p
REAL=lambda u,p:(0.05+0.0015*p) if u in ("NIFTY","FINNIFTY","MIDCPNIFTY") else (0.10+0.004*p)
i5=pd.read_sql("select key, ts, c from index_5m",con)
daily=pd.read_sql("select key, d, c from index_daily",con)
def idx_at(key,day):
    s=i5[(i5.key==key)&(i5.ts.str[:10]==day)&(i5.ts<=f"{day}T15:05:00")]
    return float(s.sort_values('ts').c.iloc[-1]) if len(s) else None
def close_on(key,day):
    s=daily[(daily.key==key)&(daily.d==day)]
    return float(s.c.iloc[0]) if len(s) else None
ex=pd.read_sql("select * from expiries where status='done'",con)
VAR={"sigma":("sigma",0.15),"d10":("sigma",0.10),"d20":("sigma",0.20),"points100":("points100",0.15)}
rows=[]; skipped={}
for _,e in ex.iterrows():
    u,exp,eve=e.underlying,e.expiry,e.eve; key=KEYS[u]
    spot,vix,settle=idx_at(key,eve),idx_at(VIX,eve),close_on(key,exp)
    if not (spot and vix and settle): skipped['no index']=skipped.get('no index',0)+1; continue
    cand=pd.read_sql("""select k.strike, k.typ, k.lot, c.ts, c.c from contracts k join candles c using(ikey)
        where k.underlying=? and k.expiry=? and substr(c.ts,1,10)=? and c.ts between ? and ?""",con,
        params=(u,exp,eve,f"{eve}T14:35:00",f"{eve}T15:05:00"))
    if cand.empty: skipped['no candles']=skipped.get('no candles',0)+1; continue
    last=cand.sort_values('ts').groupby(['strike','typ']).c.last().unstack()
    lot=int(cand.lot.iloc[0])
    dte=(pd.Timestamp(exp+' 15:30')-pd.Timestamp(eve+' 15:10')).total_seconds()/86400
    for cname,hs in (("proto",PROTO),("real",REAL)):
        chain=[]
        for k,r in last.iterrows():
            ce,pe=r.get('CE',np.nan),r.get('PE',np.nan)
            # one-sided OTM fetch: missing side is deep ITM -> give it a wide synthetic quote (never chosen as a short)
            ceb=max(ce-hs(u,ce),0.05) if ce==ce else 0; cea=ce+hs(u,ce) if ce==ce else 0
            peb=max(pe-hs(u,pe),0.05) if pe==pe else 0; pea=pe+hs(u,pe) if pe==pe else 0
            chain.append(dict(strike=float(k),ce_bid=ceb,ce_ask=cea,pe_bid=peb,pe_ask=pea))
        for v,(wing,dl) in VAR.items():
            p=CondorParams(min_vix=0.0,short_delta=dl,wing_points=P100[u])
            try: plan=C.plan_condor(chain,spot,vix,exp,dte,lot,p,wing_mode=wing)
            except C.NoTrade as ex_: skipped[str(ex_)[:30]]=skipped.get(str(ex_)[:30],0)+1; continue
            res=C.settle(plan,settle); cost=condor_charges_points(plan.to_dict())
            net=res['pnl_pts']-cost
            rows.append(dict(u=u,expiry=exp,eve=eve,vix=vix,cost=cname,variant=v,credit=plan.credit_real,maxloss=plan.max_loss_pts,
                             net=net,r=net/plan.max_loss_pts,rupees=net*lot,lot=lot,move=settle/spot-1))
df=pd.DataFrame(rows); df.to_pickle(DB+'.trades.pkl')
print('expiries analysed',ex.shape[0],'skipped',skipped)
rng=np.random.default_rng(0)
def st(g):
    x=g.r.to_numpy()
    if len(x)==0: return pd.Series(dict(n=0))
    gl=-x[x<0].sum(); boot=rng.choice(x,(5000,len(x))).mean(1) if len(x)>1 else np.array([x.mean()])
    return pd.Series(dict(n=len(x),win=(x>0).mean(),avgR=x.mean(),pf=(x[x>0].sum()/gl if gl>0 else np.inf),p=(boot<=0).mean(),
                          worstR=x.min(),rupees=g.rupees.sum()))
pd.set_option('display.width',220)
for filt,name in ((df.vix>=18,'RULE: VIX>=18'),(df.vix>0,'ALL eves (no VIX filter, info only)')):
    d=df[filt]
    print(f'\n===== {name}')
    for cost in ('proto','real'):
        x=d[d.cost==cost]
        t=x.groupby(['variant','u']).apply(st)
        pooled=x.groupby('variant').apply(st); pooled.index=pd.MultiIndex.from_product([pooled.index,['ALL']])
        print(f'-- cost {cost}'); print(pd.concat([t,pooled]).sort_index().round(3).to_string())
