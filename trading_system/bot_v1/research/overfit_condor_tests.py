import numpy as np, pandas as pd, itertools
df=pd.read_pickle('condor_grid.pkl'); base=df[df.spread==1]
rng=np.random.default_rng(0)
def boot_p(x,B=5000): return (rng.choice(x,(B,len(x))).mean(1)<=0).mean() if len(x)>1 else np.nan
def pf(x): gl=-x[x<0].sum(); return x[x>0].sum()/gl if gl>0 else np.inf
P=base[(base.delta==0.15)&(base.wing==1.0)].sort_values('day')
print('=== 1. VIX threshold sweep (primary structure 15d/1σ)')
out=[]
for th in [0,12,13,14,15,16,17,18,19,20,21,22,24,26]:
    x=P[P.vix>=th]; d=x[x.day<'2022'].r; h=x[x.day>='2022'].r
    out.append(dict(vix_min=th,n=len(x),avgR=x.r.mean(),pf=pf(x.r.values),D_n=len(d),D_avgR=d.mean(),H_n=len(h),H_avgR=h.mean(),p_all=boot_p(x.r.values)))
print(pd.DataFrame(out).round(3).to_string(index=False))
print('\n=== 2. Structure grid at VIX>=18: avg R (DESIGN | HOLDOUT)')
g=base[base.vix>=18]
t=g.groupby(['delta','wing']).apply(lambda x:f"{x[x.day<'2022'].r.mean():+.3f} | {x[x.day>='2022'].r.mean():+.3f}").unstack()
print(t.to_string())
pos=g.groupby(['delta','wing']).apply(lambda x:(x[x.day<'2022'].r.mean()>0)&(x[x.day>='2022'].r.mean()>0))
print('structures positive in BOTH periods: %d of %d'%(pos.sum(),len(pos)))
cc=g.groupby(['delta','wing']).apply(lambda x:pd.Series(dict(D=x[x.day<'2022'].r.mean(),H=x[x.day>='2022'].r.mean())))
print('Spearman DESIGN vs HOLDOUT avg R across structures: %.2f'%cc.corr('spearman').iloc[0,1])
print('\n=== 3. PBO across (threshold x structure) configs, yearly blocks')
cfgs=list(itertools.product([14,16,18,20,22],[0.10,0.15,0.20,0.25],[0.75,1.0,1.5]))
years=sorted(base.day.dt.year.unique()); M={}
for th,dl,ws in cfgs:
    x=base[(base.vix>=th)&(base.delta==dl)&(base.wing==ws)]
    M[(th,dl,ws)]=x.groupby(x.day.dt.year).r.sum().reindex(years).fillna(0)   # yearly sum of R (no-trade years = 0)
M=pd.DataFrame(M); S=len(years); lam=[]
for cmb in itertools.combinations(range(S),S//2):
    isx=list(cmb); oos=[i for i in range(S) if i not in cmb]
    a=M.iloc[isx].mean(); b=M.iloc[oos].mean(); best=a.idxmax(); w=b.rank()[best]/(len(b)+1); lam.append(np.log(w/(1-w)))
print('PBO = %.3f over %d splits, %d configs'%((np.array(lam)<0).mean(),len(lam),M.shape[1]))
print('\n=== 4. Clustered (episode) bootstrap for the rule, primary structure, VIX>=18')
x=P[P.vix>=18].copy(); x['ep']=x.day.dt.to_period('Q')
cl=x.groupby('ep').r.apply(np.array); ks=list(cl.index)
bm=[]
for _ in range(10000):
    pick=rng.choice(len(ks),len(ks)); arr=np.concatenate([cl.iloc[i] for i in pick]); bm.append(arr.mean())
bm=np.array(bm); print('trades %d in %d quarters; iid P(avgR<=0)=%.3f ; clustered P=%.3f ; 90%% CI avgR [%.3f, %.3f]'%(len(x),len(ks),boot_p(x.r.values),(bm<=0).mean(),np.percentile(bm,5),np.percentile(bm,95)))
print('\n=== 5. Does the VIX filter select better trades? permutation of the VIX label')
allr=P.r.values; k=(P.vix>=18).sum(); obs=P[P.vix>=18].r.mean()
perm=np.array([rng.choice(allr,k,replace=False).mean() for _ in range(20000)])
print('VIX>=18 avgR %.3f vs random same-size subsets mean %.3f ; one-sided p = %.3f'%(obs,perm.mean(),(perm>=obs).mean()))
print('\n=== 6. Spread x2 (primary, VIX>=18)')
s2=df[(df.spread==2)&(df.vix>=18)]
for per,m in (('DESIGN',s2.day<'2022'),('HOLDOUT',s2.day>='2022')): print(per,'n %d avgR %.3f PF %.2f P %.3f'%(m.sum(),s2[m].r.mean(),pf(s2[m].r.values),boot_p(s2[m].r.values)))
print('\n=== 7. Tail: primary, VIX>=18')
r=P[P.vix>=18].r.values; print('losses %d of %d; mean loss %.3f R; mean win %.3f R; wins needed to repay one max loss: %.1f'%((r<0).sum(),len(r),r[r<0].mean(),r[r>0].mean(),1/r[r>0].mean()))
print('worst 5 R:',np.sort(r)[:5].round(3))
