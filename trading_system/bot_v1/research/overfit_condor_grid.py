import sys, itertools, numpy as np, pandas as pd, pickle
sys.path.insert(0,'/home/claude/trading-ai/trading_system')
from bot_v1 import condor as C
from bot_v1.config import CondorParams
from bot_v1.report import condor_charges_points
D='/home/claude/realdata/data/'
opt=pd.read_pickle(D+'nifty_options_eod.pkl'); opt['trade_date']=pd.to_datetime(opt.trade_date); opt['expiry_date']=pd.to_datetime(opt.expiry_date)
n=pd.read_pickle(D+'nifty.pkl').set_index('trade_date').close; n.index=pd.to_datetime(n.index)
v=pd.read_pickle(D+'vix.pkl').set_index('trade_date').close; v.index=pd.to_datetime(v.index)
tdays=pd.DatetimeIndex(sorted(opt.trade_date.unique()))
ents=[]
for exp in sorted(opt.expiry_date.unique()):
    ob=tdays[tdays<=exp]
    if len(ob)<2: continue
    sd=ob[-1]; day=tdays[tdays<sd][-1]
    if (sd-day).days>7 or sd not in n.index or day not in n.index or day not in v.index: continue
    ch=opt[(opt.trade_date==day)&(opt.expiry_date==exp)&(opt.contracts>0)]
    piv=ch.pivot_table(index='strike',columns='option_type',values='close',aggfunc='last')
    if not {'CE','PE'}<=set(piv.columns): continue
    ents.append(dict(day=day,sd=sd,piv=piv.dropna(),spot=float(n[day]),vix=float(v[day]),settle=float(n[sd])))
def chain(piv,mult):
    hs=lambda x:mult*(0.25+0.003*x)
    return [dict(strike=float(k),ce_bid=max(r.CE-hs(r.CE),0.05),ce_ask=r.CE+hs(r.CE),pe_bid=max(r.PE-hs(r.PE),0.05),pe_ask=r.PE+hs(r.PE)) for k,r in piv.iterrows()]
rows=[]
structs=list(itertools.product([0.08,0.10,0.12,0.15,0.20,0.25],[0.5,0.75,1.0,1.5,2.0]))
for i,e in enumerate(ents):
    for mult in (1,2):
        ch=chain(e['piv'],mult)
        for dl,ws in structs:
            if mult==2 and not (dl==0.15 and ws==1.0): continue
            p=CondorParams(min_vix=0.0,short_delta=dl,wing_sigma=ws)
            try: plan=C.plan_condor(ch,e['spot'],e['vix'],str(e['sd'].date()),(e['sd']-e['day']).days+0.0,75,p,'sigma')
            except C.NoTrade: continue
            res=C.settle(plan,e['settle']); net=res['pnl_pts']-condor_charges_points(plan.to_dict())
            rows.append(dict(day=e['day'],vix=e['vix'],delta=dl,wing=ws,spread=mult,r=net/plan.max_loss_pts))
df=pd.DataFrame(rows); df.to_pickle('condor_grid.pkl'); print('rows',len(df),'entries',len(ents))
