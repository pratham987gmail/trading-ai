import sys, json; sys.path.insert(0,'/home/claude/trading-ai/trading_system')
import numpy as np, pandas as pd
from bot_v1 import condor as C
from bot_v1.config import CondorParams
from bot_v1.report import condor_charges_points
D='/home/claude/realdata/data/'
opt=pd.read_pickle(D+'nifty_options_eod.pkl')
opt['trade_date']=pd.to_datetime(opt.trade_date); opt['expiry_date']=pd.to_datetime(opt.expiry_date)
n=pd.read_pickle(D+'nifty.pkl').set_index('trade_date').close; n.index=pd.to_datetime(n.index)
v=pd.read_pickle(D+'vix.pkl').set_index('trade_date').close; v.index=pd.to_datetime(v.index)
rv=np.log(n).diff().rolling(20).std()*np.sqrt(252)*100
r5=n/n.shift(5)-1
vchg=v/v.shift(1)-1
tdays=pd.DatetimeIndex(sorted(opt.trade_date.unique()))
# entries (same for all configs)
ents=[]
for exp in sorted(opt.expiry_date.unique()):
    ob=tdays[tdays<=exp]
    if len(ob)<2: continue
    sd=ob[-1]; day=tdays[tdays<sd][-1]
    if (sd-day).days>7 or sd not in n.index or day not in n.index or day not in v.index: continue
    ch=opt[(opt.trade_date==day)&(opt.expiry_date==exp)&(opt.contracts>0)]
    piv=ch.pivot_table(index='strike',columns='option_type',values='close',aggfunc='last')
    if not {'CE','PE'}<=set(piv.columns): continue
    ents.append(dict(day=day,sd=sd,exp=exp,piv=piv.dropna(),spot=float(n[day]),vix=float(v[day]),
                     rv=float(rv.get(day,np.nan)),r5=float(r5.get(day,np.nan)),vchg=float(vchg.get(day,np.nan)),settle=float(n[sd])))
print('candidate expiry-eves',len(ents))
def chain(piv,hs):
    return [dict(strike=float(k),ce_bid=max(r.CE-hs(r.CE),0.05),ce_ask=r.CE+hs(r.CE),pe_bid=max(r.PE-hs(r.PE),0.05),pe_ask=r.PE+hs(r.PE)) for k,r in piv.iterrows()]
PROTO=lambda x:0.25+0.003*x
REAL=lambda x:0.03 if x<=20 else (0.05 if x<=40 else (0.07 if x<=80 else 0.15))
vrp=lambda e:e['vix']>=1.25*e['rv'] and e['vix']>=13
calm=lambda e:abs(e['r5'])<0.02
CFG={
 'V00':(lambda e:e['vix']>=18, dict()),
 'V01':(lambda e:e['vix']>=15, dict(min_vix=15)),
 'V02':(lambda e:e['vix']>=13, dict(min_vix=13)),
 'V03':(vrp, dict(min_vix=13)),
 'V04':(lambda e:vrp(e) and e['vix']>=15, dict(min_vix=15)),
 'V05':(lambda e:e['vix']>=18 and calm(e), dict()),
 'V06':(lambda e:e['vix']>=18 and e['vchg']<0.10, dict()),
 'V07':(lambda e:vrp(e) and calm(e), dict(min_vix=13)),
 'V08':(lambda e:e['vix']>=18, dict(short_delta=0.10)),
 'V09':(lambda e:e['vix']>=18, dict(short_delta=0.20)),
 'V10':(lambda e:e['vix']>=18, dict(wing_sigma=0.5)),
 'V11':(vrp, dict(min_vix=13,short_delta=0.10)),
}
rows=[]
for name,(flt,kw) in CFG.items():
    p=CondorParams(**kw)
    for e in ents:
        if not np.isfinite(e['rv']) or not flt(e): continue
        for cost,hs in (('proto',PROTO),('real',REAL)):
            try: plan=C.plan_condor(chain(e['piv'],hs),e['spot'],e['vix'],str(e['exp'].date()),(e['sd']-e['day']).days+0.0,75,p,'sigma')
            except C.NoTrade: continue
            res=C.settle(plan,e['settle']); net=res['pnl_pts']-condor_charges_points(plan.to_dict())
            rows.append(dict(cfg=name,cost=cost,entry=e['day'],r=net/plan.max_loss_pts,pts=net,maxloss=plan.max_loss_pts))
df=pd.DataFrame(rows); df.to_pickle('/home/claude/realdata/condor_v2/trades.pkl')
rng=np.random.default_rng(0)
def st(x):
    gl=-x[x<0].sum(); boot=rng.choice(x,(10000,len(x))).mean(1)
    return dict(n=len(x),win=(x>0).mean(),avgR=x.mean(),pf=x[x>0].sum()/gl if gl>0 else np.inf,p=(boot<=0).mean(),worst=x.min())
out=[]
for (c,cost),g in df.groupby(['cfg','cost']):
    for per,m in (('DESIGN',g.entry<'2022-01-01'),('HOLDOUT',g.entry>='2022-01-01')):
        out.append(dict(cfg=c,cost=cost,period=per,**st(g.r[m].to_numpy())))
T=pd.DataFrame(out); T.to_csv('/home/claude/realdata/condor_v2/table.csv',index=False)
pd.set_option('display.width',200)
P=T[T.cost=='proto'].pivot(index='cfg',columns='period',values=['n','avgR','pf','p'])
print(P.round(3).to_string())
d=T[(T.cost=='proto')&(T.period=='DESIGN')&(T.n>=40)].sort_values('avgR',ascending=False)
sel=d.iloc[0].cfg; print('\nSELECTED on DESIGN:',sel)
print(T[(T.cfg==sel)].round(3).to_string(index=False))
print(T[(T.cfg=='V00')].round(3).to_string(index=False))
