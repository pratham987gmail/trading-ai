import numpy as np, pandas as pd
D='/home/claude/realdata/data/'
o=pd.read_pickle(D+'nifty_options_eod.pkl'); o['trade_date']=pd.to_datetime(o.trade_date); o['expiry_date']=pd.to_datetime(o.expiry_date)
n=pd.read_pickle(D+'nifty.pkl').set_index('trade_date').close; n.index=pd.to_datetime(n.index)
v=pd.read_pickle(D+'vix.pkl').set_index('trade_date').close; v.index=pd.to_datetime(v.index)
px=o.set_index(['trade_date','expiry_date','strike','option_type']).close.sort_index()
px=px[~px.index.duplicated()]
days=pd.DatetimeIndex(sorted(set(o.trade_date)&set(n.index)&set(v.index)))
exps=np.array(sorted(o.expiry_date.unique()))
def settle_day(e):
    s=days[days<=e]; return s[-1] if len(s) else None
def run(sl=0.90,tp=0.40,maxbars=7,hs=lambda p:0.25+0.003*p,lot=75):
    trades=[]; i=0
    while i<len(days):
        d=days[i]; spot=n[d]; iv=v[d]/100
        fut=[e for e in exps if pd.Timestamp(e)>d]
        if not fut: break
        e=pd.Timestamp(fut[0]); sd=settle_day(e); dte=(e-d).days
        if not (2<=dte<=6 and 0.11<=iv<=0.35) or sd is None or sd<=d: i+=1; continue
        try: chain=px.loc[(d,e)]
        except KeyError: i+=1; continue
        ks=sorted(set(k for k,t in chain.index if t=='CE')&set(k for k,t in chain.index if t=='PE'))
        if not ks: i+=1; continue
        k=min(ks,key=lambda x:abs(x-spot)); ce,pe=chain[(k,'CE')],chain[(k,'PE')]
        credit=ce-hs(ce)+pe-hs(pe)
        last=(ce,pe); j=i; reason=None
        while True:
            j+=1
            if j>=len(days): reason='END'; break
            dd=days[j]
            if dd==sd:
                s=n[dd]; cost=max(s-k,0)+max(k-s,0); reason='EXPIRY'; break
            try: c2,p2=px.loc[(dd,e,k,'CE')],px.loc[(dd,e,k,'PE')]
            except KeyError: c2,p2=last
            last=(c2,p2); cost=c2+hs(c2)+p2+hs(p2); pnl=credit-cost
            if pnl>=tp*credit: reason='TARGET'; break
            if pnl<=-sl*credit: reason='STOP'; break
            if j-i>=maxbars: reason='TIME'; break
        if reason=='END': break
        stt=0.001*(ce+pe)
        pnl=credit-cost-stt-4*20*1.18/lot
        trades.append(dict(entry=d,exit=days[j],dte=dte,vix=iv*100,k=k,credit=credit,pnl=pnl,reason=reason,move=(n[days[j]]/spot-1)))
        i=j+1
    return pd.DataFrame(trades)
for name,h in [('protocol spread',lambda p:0.25+0.003*p),('tight real-2026 spread',lambda p:0.05+0.001*p)]:
    t=run(hs=h); t['rup']=t.pnl*75
    print(f"\n== {name}: {len(t)} trades {t.entry.min().date()}..{t.entry.max().date()}")
    print(' win %.1f%%  PF %.2f  total ₹%s/lot  worst trade ₹%s  avg credit %.0f pts' % (100*(t.pnl>0).mean(), t.pnl[t.pnl>0].sum()/-t.pnl[t.pnl<0].sum(), f"{t.rup.sum():,.0f}", f"{t.rup.min():,.0f}", t.credit.mean()))
    print(' exits', t.reason.value_counts().to_dict())
    y=t.groupby(t.entry.dt.year).agg(n=('pnl','size'),win=('pnl',lambda x:(x>0).mean()),rupees=('rup','sum'),worst=('rup','min')).round(2)
    print(y.to_string())
    s=t.sort_values('entry'); eq=s.rup.cumsum(); print(' max drawdown ₹%s per lot' % f"{(eq-eq.cummax()).min():,.0f}")
    print(' 2023-01..2024-10 (their window):', len(s[(s.entry>='2023-01-01')&(s.entry<'2024-10-01')]), 'trades, ₹%s' % f"{s[(s.entry>='2023-01-01')&(s.entry<'2024-10-01')].rup.sum():,.0f}")
    print(' 5 worst:'); print(s.nsmallest(5,'rup')[['entry','exit','vix','credit','pnl','rup','reason','move']].round(3).to_string(index=False))
