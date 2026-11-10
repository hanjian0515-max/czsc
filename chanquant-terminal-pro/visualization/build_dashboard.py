"""Build an offline symbol-universe visualization; never fabricate market data."""
from __future__ import annotations
import csv
import json
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
CAMPS=[
    {"name":"宏观宽基","color":"#52d6c8","description":"指数与宽基ETF，作为市场环境与风格的观察层。"},
    {"name":"科技进攻","color":"#8e9dff","description":"半导体、通信、电子制造与自动化；仅主题归类。"},
    {"name":"新能源与电网","color":"#e5c36a","description":"新能源车、光伏、风电与电网需进一步区分。"},
    {"name":"周期与资源","color":"#be9df9","description":"资源、制造、运输与券商周期；不表示已进入主升。"},
    {"name":"消费与防守","color":"#e5a0b7","description":"消费、医药、银行保险与公用事业观察；不保证低风险。"},
]


def build():
    with (ROOT/'research/universe_66_source.csv').open(encoding='utf-8',newline='') as file:
        rows=list(csv.DictReader(file))
    assert len(rows)==66 and [int(r['rank']) for r in rows]==list(range(1,67))
    assert len({r['symbol'] for r in rows})==66
    expected={'stock':43,'etf':11,'index':4,'sector':8}
    assert dict(Counter(r['asset_type'] for r in rows))==expected
    assert all(r['symbol']==r['code']+'.'+r['symbol'].split('.')[-1] and len(r['code'])==6 for r in rows)
    items=[]
    for r in rows:
        market=r['symbol'].split('.')[-1]
        tradable=r['tradable']=='true'
        assert tradable==(r['asset_type'] in {'stock','etf'})
        assert (market=='TDX')==(r['asset_type']=='sector')
        items.append({'rank':int(r['rank']),'code':r['code'],'symbol':r['symbol'],'name':r['name'],
                      'market':market,'type':r['asset_type'],'camp':r['camp'],'tags':r['themes'].split('|'),
                      'tradable':tradable})
    assert sum(r['tradable'] for r in items)==54
    assert next(i for i in items if i['rank']==2)['symbol']=='000001.SH'
    assert next(i for i in items if i['rank']==21)['symbol']=='399300.SZ'
    payload={'items':items,'camps':CAMPS,'source':'用户提供的两张66标的截图','quote_status':'not_loaded',
             'asof':None,'classification':'按业务主题暂分，未核验当前强弱，不代表作者原始分类或投资评级'}
    template=(ROOT/'visualization/index.template.html').read_text(encoding='utf-8')
    if template.count('__CQ_DATA__')!=1: raise ValueError('HTML template must have exactly one data placeholder')
    content=template.replace('__CQ_DATA__',json.dumps(payload,ensure_ascii=False).replace('<','\\u003c'))
    target=ROOT/'visualization/dashboard.html'
    target.write_text(content,encoding='utf-8')
    (ROOT/'visualization/universe_66.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    # Keep the project's existing configuration schema, retaining the explicit .TDX namespace.
    config=ROOT/'config/universe.csv'
    old=config.read_text(encoding='utf-8') if config.exists() else ''
    if old.strip() not in {'','symbol,name,camp,asset_type'}:
        existing=list(csv.DictReader(old.splitlines()))
        if [(r['symbol'],r['name'],r['camp'],r['asset_type']) for r in existing]!=[(r['symbol'],r['name'],r['camp'],r['asset_type']) for r in rows]:
            raise ValueError('Existing universe configuration differs; refusing to overwrite')
    with config.open('w',encoding='utf-8',newline='') as file:
        writer=csv.DictWriter(file,fieldnames=['symbol','name','camp','asset_type'])
        writer.writeheader();writer.writerows({k:r[k] for k in writer.fieldnames} for r in rows)
    print(f'Built {target}; 66 items, types={expected}, camps={dict(Counter(r["camp"] for r in rows))}')


if __name__=='__main__': build()
