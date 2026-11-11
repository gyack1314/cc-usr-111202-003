"""检查公开分类树及原始事件归属。"""
from collections import Counter
from .io import unique,identity,integer

def summarize(v):
    nodes=v['tree'];known=unique(nodes,'id');parents={n['id']:n['parent'] for n in nodes}
    roots=[x for x,p in parents.items() if p is None]
    if len(roots)!=1:raise ValueError('需要唯一根')
    for x in known:
        chain=set();at=x
        while at is not None:
            if at in chain or at not in known:raise ValueError('树含环或未知父节点')
            chain.add(at);at=parents[at]
    leaves=known-{p for p in parents.values() if p is not None}
    unique(v['events'],'event_id');counts=Counter();people=set()
    for e in v['events']:
        people.add(identity(e['subject_id']));integer(e['event_day'])
        if e['leaf'] not in leaves:raise ValueError('事件分类必须是叶')
        counts[e['leaf']]+=1
    return {'raw_people':len(people),'raw_events':len(v['events']),'raw_leaf_events':{x:counts[x] for x in sorted(leaves)}}
