import sys, re, time, pickle, numpy as np, pandas as pd
sys.argv=['x']; sys.path.insert(0,'/home/claude/realdata/equity_momentum'); sys.path.insert(0,'/home/claude/realdata/factors')
src=open('/home/claude/realdata/factors/factors.py').read().split('if __name__')[0]
G={'__name__':'F','__file__':'factors.py'}; exec(compile(src,'factors.py','exec'),G)
E=G['E']; syms=E.syms; T,N=E.T,E.N; R0=E.R0; LP=E.LP; present=E.present; dates=pd.to_datetime(E.dates); months=dates.month
pat=re.compile(r'BEES|ETF|LIQUID|CASE|GOLD|SILVER|NIFTY|SENSEX|JUNIOR|^N100|^MON100|^MAFANG|^HNG',re.I)
etf=np.array([bool(pat.search(str(s))) for s in syms])
UNIV={}
for t in E.UNIV:
    ok=present[t]&(E.hist[t]>=250)&~etf
    m=np.where(ok,E.liq_rank(t),np.nan); o=np.argsort(-np.nan_to_num(m,nan=-1))
    UNIV[t]=np.array([i for i in o[:200] if not np.isnan(m[i])])
Pm=E.Pm
def filt(sma):
    if sma is None: return np.ones(T,bool)
    s=pd.Series(Pm).rolling(sma).mean().values; return Pm>=s
VOL={}
def vol(t,u):
    return R0[t-251:t+1,u].astype(np.float64).std(0)*np.sqrt(252)
def score(kind,t,u):
    v=vol(t,u); r=lambda L:(LP[t,u]-LP[t-L,u]).astype(np.float64)
    zs=G['zs']
    if kind=='m6_12': return 0.5*zs(r(126)/v)+0.5*zs(r(252)/v)
    if kind=='m12': return r(252)/v
    if kind=='m6': return r(126)/v
    if kind=='m3_6': return 0.5*zs(r(63)/v)+0.5*zs(r(126)/v)
    if kind=='raw12_1': return (LP[t-21,u]-LP[t-252,u]).astype(np.float64)
    if kind=='ew': return np.zeros(len(u))
    if kind.startswith('rand'):
        rng=np.random.default_rng(int(kind[4:])*100000+t); return rng.random(len(u))
    raise ValueError(kind)
def run(kind,topn,sched,f,cost=0.00175):
    rets=np.zeros(T); w=np.zeros(N); cash=1.0; started=False; invested=False
    exec_map={t+1:t for t in UNIV}
    for d in range(T):
        if started:
            g=w*(1+R0[d]); port=g.sum()+cash; rets[d]=port-1; w=g/port; cash=cash/port
        if d not in exec_map: continue
        t=exec_map[d]
        if not f[t]: tgt=np.zeros(N); invested=False
        elif months[t] in sched or not invested:
            u=UNIV[t]; u=u[present[d,u]]; s=score(kind,t,u); m=np.isfinite(s)
            k=len(u) if topn=='all' else topn
            names=u[m][np.argsort(-s[m],kind='stable')[:k]]
            tgt=np.zeros(N); tgt[names]=1.0/len(names); invested=True
        else: continue
        if not started and np.abs(tgt-w).sum()>0: started=True
        c=cost*np.abs(tgt-w).sum(); rets[d]=(1+rets[d])*(1-c)-1
        w=tgt.copy(); cash=1-tgt.sum()
    return pd.Series(rets,index=dates)
NIFTY=pd.Series(E.rm,index=dates); BEES=pd.Series(E.bees,index=dates)
if __name__=='__main__':
    t0=time.time(); r=run('m6_12',30,{6,12},filt(200)); print('one run',round(time.time()-t0,1),'s')
    def cagr(x,a,b):
        x=x[a:b]; return (1+x).prod()**(252/len(x))-1
    print('F03 stocks-only design %.3f holdout %.3f'%(cagr(r,'2006','2016'),cagr(r,'2017','2026')))
