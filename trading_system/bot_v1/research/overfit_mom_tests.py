import itertools, numpy as np, pandas as pd, pickle
from scipy.stats import norm
from mom_harness import *
def stats(x,a,b):
    x=x[a:b]; n=len(x); c=(1+x).prod()**(252/n)-1; s=x.mean()/x.std()*np.sqrt(252) if x.std()>0 else 0
    eq=(1+x).cumprod(); dd=(eq/eq.cummax()-1).min(); return c,s,dd
grid=list(itertools.product(['m6_12','m12','m6','m3_6','raw12_1'],[10,20,30,50],[(6,12),(3,6,9,12),(12,)],[None,150,200,250]))
F={s:filt(s) for s in [None,150,200,250]}
import os
R=pickle.load(open('mom_grid.pkl','rb')) if os.path.exists('mom_grid.pkl') else {}
for g in [g for g in grid if g not in R]:
    R[g]=run(g[0],g[1],set(g[2]),F[g[3]])
pickle.dump(R,open('mom_grid.pkl','wb'))
nb=BEES; nf=NIFTY
rows=[]
for g,r in R.items():
    dc,ds,dd=stats(r,'2006','2016'); hc,hs,hd=stats(r,'2017','2026')
    rows.append(dict(sig=g[0],top=g[1],reb={2:'semi',4:'qtr',1:'annual'}[len(g[2])],sma=g[3] or 0,D_cagr=dc,D_sh=ds,H_cagr=hc,H_sh=hs,H_dd=hd))
T_=pd.DataFrame(rows)
bH=stats(nb,'2017','2026')[0]; bD=stats(nb,'2006','2016')[0]
print('NIFTYBEES (total return) design %.3f holdout %.3f ; NIFTY price holdout %.3f'%(bD,bH,stats(nf,'2017','2026')[0]))
f03=T_[(T_.sig=='m6_12')&(T_.top==30)&(T_.reb=='semi')&(T_.sma==200)].iloc[0]
print('F03 row:',{k:(round(v,3) if isinstance(v,float) else v) for k,v in f03.to_dict().items()})
print('grid size',len(T_))
print('share of grid with holdout CAGR > NIFTYBEES+2%%: %.2f'%((T_.H_cagr>bH+0.02).mean()))
print('F03 holdout rank (1=best): %d of %d ; design rank %d'%((T_.H_cagr>f03.H_cagr).sum()+1,len(T_),(T_.D_sh>f03.D_sh).sum()+1))
print('\nHoldout CAGR by each dimension (median over the rest of the grid):')
for c in ['sig','top','reb','sma']: print(T_.groupby(c).H_cagr.median().round(3).to_dict())
# design->holdout rank correlation
print('\nSpearman design Sharpe vs holdout Sharpe: %.2f'%T_[['D_sh','H_sh']].corr('spearman').iloc[0,1])
best=T_.sort_values('D_sh',ascending=False).head(5); print('Top-5 by DESIGN Sharpe and their HOLDOUT:\n',best.round(3).to_string(index=False))
# ---- PBO (CSCV) on monthly returns 2006-2026
M=pd.DataFrame({i:(1+r['2006':'2026']).resample('ME').prod()-1 for i,r in enumerate(R.values())})
S=16; blocks=np.array_split(np.arange(len(M)),S); rng=np.random.default_rng(0)
import math; combos=list(itertools.combinations(range(S),S//2)); rng.shuffle(combos); lam=[]
for cmb in combos[:3000]:
    isx=np.concatenate([blocks[i] for i in cmb]); oos=np.concatenate([blocks[i] for i in range(S) if i not in cmb])
    sh=lambda X:X.mean()/X.std()
    a=sh(M.iloc[isx]); b=sh(M.iloc[oos]); best=a.idxmax(); w=(b.rank()[best])/(len(b)+1); lam.append(np.log(w/(1-w)))
lam=np.array(lam); print('\nPBO (prob. the in-sample best is below median out-of-sample): %.3f'%(lam<0).mean())
# ---- walk-forward reselection: each year pick best Sharpe config on prior 5y
wf=[]; keys=list(R)
for y in range(2011,2027):
    tr={k:R[k][str(y-5):str(y-1)] for k in keys}; k=max(keys,key=lambda k:tr[k].mean()/tr[k].std()); wf.append(R[k][str(y)])
wf=pd.concat(wf); print('Walk-forward re-selected 2011-2026 CAGR %.3f vs F03 %.3f vs NIFTYBEES %.3f'%(stats(wf,'2011','2026')[0],stats(R[('m6_12',30,(6,12),200)],'2011','2026')[0],stats(nb,'2011','2026')[0]))
# ---- placebos
ew=run('ew','all',{6,12},F[200]); print('\nPLACEBO equal-weight all 200 + SMA200 filter: design %.3f holdout %.3f'%(stats(ew,'2006','2016')[0],stats(ew,'2017','2026')[0]))
ewn=run('ew','all',{6,12},F[None]); print('PLACEBO equal-weight all 200, no filter: design %.3f holdout %.3f'%(stats(ewn,'2006','2016')[0],stats(ewn,'2017','2026')[0]))
m_nf=R[('m6_12',30,(6,12),None)]; print('F03 WITHOUT trend filter: design %.3f holdout %.3f'%(stats(m_nf,'2006','2016')[0],stats(m_nf,'2017','2026')[0]))
rnd=[run(f'rand{i}',30,{6,12},F[200]) for i in range(60)]
rh=np.array([stats(x,'2017','2026')[0] for x in rnd]); rd=np.array([stats(x,'2006','2016')[0] for x in rnd])
print('RANDOM 30 stocks + filter (60 seeds): design median %.3f [p95 %.3f], holdout median %.3f [p95 %.3f]; F03 beats %.0f%% / %.0f%% of random'%(np.median(rd),np.percentile(rd,95),np.median(rh),np.percentile(rh,95),100*(rd<f03.D_cagr).mean(),100*(rh<f03.H_cagr).mean()))
# ---- cost stress
for k in (2,3,5):
    x=run('m6_12',30,{6,12},F[200],cost=0.00175*k); print('costs x%d: design %.3f holdout %.3f'%(k,stats(x,'2006','2016')[0],stats(x,'2017','2026')[0]))
# ---- deflated Sharpe (full period daily) given N trials
r=R[('m6_12',30,(6,12),200)]['2006':'2026']; sr=r.mean()/r.std(); sk=r.skew(); ku=r.kurt()+3; n=len(r)
srs=np.array([x['2006':'2026'].mean()/x['2006':'2026'].std() for x in R.values()])
for Ntr in (60,240,300):
    g=0.5772; sr0=np.sqrt(srs.var())*((1-g)*norm.ppf(1-1/Ntr)+g*norm.ppf(1-1/(Ntr*np.e)))
    dsr=norm.cdf((sr-sr0)*np.sqrt(n-1)/np.sqrt(1-sk*sr+(ku-1)/4*sr**2)); print('Deflated Sharpe prob (N=%d trials): %.3f  [annual SR %.2f]'%(Ntr,dsr,sr*np.sqrt(252)))
# excess vs NIFTYBEES total return, holdout & rolling 3y
ex=(1+r).resample('YE').prod()-(1+nb['2006':'2026']).resample('YE').prod(); print('\nYears beating NIFTYBEES: %d of %d'%((ex>0).sum(),len(ex)))
print('Excess by year:',(100*ex).round(1).set_axis(ex.index.year).to_dict())
T_.to_csv('mom_grid.csv',index=False)
