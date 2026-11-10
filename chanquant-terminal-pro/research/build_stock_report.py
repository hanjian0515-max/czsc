"""Assemble 43 conditional stock research cards without inventing live technical conclusions."""
from __future__ import annotations
import csv
import html
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
FIELDS=['symbol','name','group','business_focus','drivers','reference_symbols','structure_focus','entry_condition','invalid_condition','company_risk','crowding_group']


def build():
    source=list(csv.DictReader((ROOT/'research/universe_66_source.csv').open(encoding='utf-8')))
    pool={x['symbol']:x for x in source};stocks=[x for x in source if x['asset_type']=='stock']
    combined=[]
    for filename in ['stocks_tech.json','stocks_energy.json','stocks_cycle.json','stocks_defensive.json']:
        combined.extend(json.loads((ROOT/'research'/filename).read_text(encoding='utf-8')))
    assert len(combined)==43 and len({r['symbol'] for r in combined})==43
    mapping={r['symbol']:r for r in combined}
    assert set(mapping)=={r['symbol'] for r in stocks}
    records=[]
    for row in stocks:
        detail=mapping[row['symbol']]
        assert all(k in detail for k in FIELDS)
        assert detail['name']==row['name']
        assert len(detail['drivers'])==3 and all(isinstance(x,str) and x.strip() for x in detail['drivers'])
        assert len(detail['reference_symbols'])<=3 and set(detail['reference_symbols'])<=set(pool)
        assert detail['symbol'] not in detail['reference_symbols']
        assert all(isinstance(detail[k],str) and detail[k].strip() for k in FIELDS if k not in {'drivers','reference_symbols'})
        records.append({**detail,'rank':int(row['rank']),'camp':row['camp'],
                        'current_status':'最新K线未取得，当前结构未判定','data_asof':None,
                        'reference_names':[f"{pool[s]['name']}（{s}）" for s in detail['reference_symbols']]})
    metadata={'requested_time':'最新已收盘行情','actual_market_data_asof':None,'current_technical_analysis_available':False,
              'source':'用户名单及通用业务/结构研究框架；未读取最新K线、公告或财报，不归为大师兄原文总结',
              'stocks':records}
    (ROOT/'research/stock_analysis_43.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    with (ROOT/'research/stock_analysis_43.csv').open('w',encoding='utf-8-sig',newline='') as f:
        fields=['symbol','name','group','camp','current_status','business_focus','drivers','reference_symbols','structure_focus','entry_condition','invalid_condition','company_risk','crowding_group']
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
        writer.writerows({k:('；'.join(r[k]) if isinstance(r[k],list) else r[k]) for k in fields} for r in records)
    lines=['# 43只个股：业务驱动与缠论逐股研究卡','',
           '**本页没有最新行情结论。** 用户要求最新已收盘分析，但行情请求被云代理拒绝；未取得当前K线、最新公告及财报。以下是分股票的研究问题、条件化结构检查和失效判据，不是当前买卖评级或价格目标。','',
           '操作框架：日线只看环境，30分钟为主操作周期，5分钟只作当时可知的确认。三买需明确中枢、离开及次级别回抽不回中枢；二买要先明确前一买点及反弹、回调的完整关系。一次前高突破、MACD正柱或单个底分型不能各自替代全部条件。','',
           '原先回测的指数历史正收益不能直接移植到这些股票。同日23同步为本项目事件规则，尚非已核验作者原文定义。','']
    summaries=[]
    for r in records:
        lines += [f"## {r['name']} · {r['symbol']}",'',f"- 细分观察：{r['group']}；{r['camp']}。",f"- 当前状态：{r['current_status']}。",f"- 业务线索：{r['business_focus']}",
                  '- 待核验驱动：'+'；'.join(r['drivers']),f"- 环境参照：{'、'.join(r['reference_names']) or '名单内无合适同类；不强行认定同行'}。",
                  f"- 30M重点：{r['structure_focus']}",f"- 买点成立条件：{r['entry_condition']}",f"- 失效条件：{r['invalid_condition']}",
                  f"- 业务不确定性：{r['company_risk']}",f"- 重叠敞口：{r['crowding_group']}",'']
        summaries.append((r['symbol'],r['name'],r['group']))
    lines += ['## 升级为当前结构结论所需证据','',
              '每只需同一截止时点的日线、30分钟及5分钟真实OHLCV，统一市场、复权和收盘时间标签；30分钟建议准备500～1000根以上并检查结构充分性，5分钟需覆盖候选触发与此前结构。',
              '逐股输出应包含：数据截至时间、当前已确认结构、中枢对象及[ZD,ZG]、二/三买发生和确认时点、撤销条件、数据缺口；缺数据应写“未判定”，不能用布尔False冒充已检查未命中。',
              '66项中的12个指数/板块仅作环境参照，不作股票下单；8个880本地代码不能当北交所股票。']
    (ROOT/'research/stock_analysis_43.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    cards=[]
    for r in records:
        esc=html.escape
        def field(title,text):return f'<section class="field"><h3>{esc(title)}</h3><p>{esc(text)}</p></section>'
        body=field('业务观察线索',r['business_focus'])+'<section class="field"><h3>需核验的驱动</h3><ul>'+''.join(f'<li>{esc(v)}</li>' for v in r['drivers'])+'</ul></section>'
        body+=field('环境参照（不代表走势同步）','、'.join(r['reference_names']) or '名单内没有合适同类，不强行认定同行')
        for title,key in [('30分钟结构重点','structure_focus'),('买点成立条件（非当前判断）','entry_condition'),('原假设失效条件','invalid_condition'),('业务不确定性','company_risk'),('敞口重叠','crowding_group')]:body+=field(title,r[key])
        search=' '.join([r['name'],r['symbol'],r['camp'],r['group'],r['crowding_group']])
        cards.append(f'<details class="stock" id="{r["symbol"].replace(".","-")}" data-camp="{esc(r["camp"],quote=True)}" data-search="{esc(search.lower(),quote=True)}"><summary><span class="code">{esc(r["symbol"])}</span><strong>{esc(r["name"])}</strong><span class="group">{esc(r["group"])}</span><span class="status">当前结构未判定</span></summary><div class="content">{body}</div></details>')
    camps=list(dict.fromkeys(r['camp'] for r in records))
    options=''.join(f'<option value="{html.escape(c)}">{html.escape(c)}</option>' for c in camps)
    page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>43只个股 · 缠论研究卡</title>
<style>*{box-sizing:border-box}body{margin:0;background:#0b1420;color:#dce8f4;font:15px/1.8 system-ui,"Noto Sans CJK SC",sans-serif}.wrap{max-width:1120px;margin:auto;padding:32px 22px}h1{font-size:29px;margin:0 0 12px;color:#f3f7fb}.intro{color:#9db1c7}.note{border:1px solid #674e27;background:#2b2418;color:#dac7a6;padding:17px;border-radius:10px}.toolbar{position:sticky;top:0;background:#0b1420ec;backdrop-filter:blur(10px);padding:17px 0;display:flex;flex-wrap:wrap;gap:9px;z-index:2}input,select,button{background:#132438;color:#ccdeed;border:1px solid #2d435b;border-radius:6px;padding:10px;font:inherit;font-size:13px}input{flex:1;min-width:180px}button{cursor:pointer}.count{color:#64dabb;margin:0 0 14px}.stock{margin:10px 0;border:1px solid #29405a;border-radius:9px;background:#101e2d;scroll-margin-top:90px}.stock[hidden]{display:none}summary{cursor:pointer;display:flex;align-items:center;gap:14px;padding:16px;flex-wrap:wrap}summary strong{font-size:17px}.code{font:13px ui-monospace,monospace;color:#73d7bc}.group{color:#9bb3c9;font-size:13px}.status{font-size:11px;color:#c6ab77;margin-left:auto}.content{border-top:1px solid #253b51;padding:18px 22px;display:grid;grid-template-columns:1fr 1fr;gap:18px}.field h3{font-size:13px;color:#68d8bc;margin:0 0 5px}.field p,.field ul{font-size:13px;color:#aec2d6;margin:0;line-height:1.9}.field ul{padding-left:20px}.foot{font-size:12px;color:#8299b2;padding:24px 0}a{color:#75d7c0}@media(max-width:650px){.wrap{padding:24px 14px}h1{font-size:23px}.content{grid-template-columns:1fr;padding:16px}summary{gap:8px}.status{width:100%;margin:0}.toolbar{position:static}}@media print{body{background:white;color:black}.toolbar{display:none}.content{display:grid!important}details>summary{list-style:none}.stock{break-inside:avoid}.field p,.field ul,.group,.status,.intro{color:#333}.field h3,.code{color:#145f51}}</style>
<main class="wrap"><h1>43只个股：业务驱动与缠论研究卡</h1><p class="intro">30M 主操作结构 · 5M 因果确认 · 日线环境参考。按原截图次序整理，可搜索、筛选、展开对照。</p>
<div class="note"><strong>当前没有最新行情结论。</strong> 行情源被网络代理拒绝，尚未取得当前K线、公告及财报。下面是业务线索和条件化结构检查，不能当作当前买卖评级。此前指数回测收益也不能迁移为这些个股的预测。</div>
<div class="toolbar"><input id="query" placeholder="搜索代码、名称、细分行业或阵营"><select id="camp"><option value="all">全部个股</option>__OPTIONS__</select><button id="open-all">展开当前列表</button><button id="close-all">收起全部</button><button id="reset">重置</button></div><p class="count" id="count">43 / 43 只个股</p>
__CARDS__
<p class="foot">业务主题不是最新分部收入结论，行业风险不是当前经营恶化判断。本稿不冒称大师兄原文；现行CZSC辅助信号与严格递归结构仍需区分。要形成现价区间与当前买点，需真实且统一时点的日线/30M/5M数据。</p></main>
<script>const cards=[...document.querySelectorAll('.stock')],q=document.getElementById('query'),camp=document.getElementById('camp');function render(){let n=0;for(const c of cards){const show=(camp.value==='all'||c.dataset.camp===camp.value)&&c.dataset.search.includes(q.value.trim().toLowerCase());c.hidden=!show;if(show)n++}document.getElementById('count').textContent=n+' / 43 只个股'}q.addEventListener('input',render);camp.addEventListener('change',render);document.getElementById('open-all').onclick=()=>cards.filter(c=>!c.hidden).forEach(c=>c.open=true);document.getElementById('close-all').onclick=()=>cards.forEach(c=>c.open=false);document.getElementById('reset').onclick=()=>{q.value='';camp.value='all';render()};if(location.hash){const c=document.querySelector(location.hash);if(c&&c.classList.contains('stock'))c.open=true}</script></html>'''
    (ROOT/'research/stock_analysis_43.html').write_text(page.replace('__OPTIONS__',options).replace('__CARDS__',''.join(cards)),encoding='utf-8')
    print('Built43 complete stock research cards; latest technical status remains unassessed')


if __name__=='__main__':build()
