"""Loopback-only desktop stock alert application. Read-only, no broker/order API."""
from __future__ import annotations
import argparse
import csv
import io
import json
from pathlib import Path
import secrets
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

ROOT=Path(__file__).resolve().parent
APP_ID='chanquant-local-stock-alerts-v1'
APP_VERSION='1.2.0'


def load_universe():
    with (ROOT/'research/universe_66_source.csv').open(encoding='utf-8',newline='') as f:
        stocks=[dict(r) for r in csv.DictReader(f) if r['asset_type']=='stock']
    if len(stocks)!=43 or len({r['symbol'] for r in stocks})!=43:
        raise ValueError('个股名单应为43只且代码不重复')
    path=ROOT/'research/stock_analysis_43.json'
    research={r['symbol']:r for r in json.loads(path.read_text(encoding='utf-8'))['stocks']} if path.exists() else {}
    return [{**r,'themes':r['themes'].split('|'),'research':research.get(r['symbol'])} for r in stocks]


class AlertHTTPServer(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True

    def __init__(self,address,monitor,store,universe,token=None):
        self.monitor=monitor;self.store=store;self.universe=universe
        self.token=token or secrets.token_urlsafe(32)
        super().__init__(address,AlertHandler)

    def server_bind(self):
        # Winsock SO_REUSEADDR can let another process bind an active listener.
        # Exclusive binding keeps repeated desktop launches on one instance.
        if sys.platform=='win32':
            self.allow_reuse_address=False
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()


class AlertHandler(BaseHTTPRequestHandler):
    server_version='ChanQuantLocal/1.0'

    def log_message(self,format,*args):
        # No request headers, session token or filesystem paths in HTTP logs.
        pass

    def _headers(self,status,kind='application/json; charset=utf-8'):
        self.send_response(status);self.send_header('Content-Type',kind)
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")

    def _json(self,value,status=200):
        self._headers(status);self.end_headers()
        self.wfile.write(json.dumps(value,ensure_ascii=False,default=str).encode('utf-8'))

    def _valid_host(self):
        port=self.server.server_address[1]
        return self.headers.get('Host') in {f'127.0.0.1:{port}',f'localhost:{port}'}

    def _authorized(self):
        if not self._valid_host():
            self._json({'ok':False,'error':'只允许本机访问'},403);return False
        origin=self.headers.get('Origin')
        port=self.server.server_address[1]
        if origin and origin not in {f'http://127.0.0.1:{port}',f'http://localhost:{port}'}:
            self._json({'ok':False,'error':'不接受其他页面的跨域控制请求'},403);return False
        supplied=self.headers.get('X-CQ-Token','')
        if not secrets.compare_digest(supplied,self.server.token):
            self._json({'ok':False,'error':'会话已失效，请从桌面启动入口重新打开'},403);return False
        return True

    def do_GET(self):
        parsed=urllib.parse.urlparse(self.path)
        if not self._valid_host():self._json({'ok':False,'error':'只允许本机访问'},403);return
        if parsed.path=='/api/health':
            self._json({'app':APP_ID,'version':APP_VERSION,'read_only':True});return
        if parsed.path in {'/','/index.html'}:
            template=(ROOT/'alerts/web.html').read_text(encoding='utf-8')
            if template.count('__CQ_TOKEN__')!=1:
                self._json({'ok':False,'error':'前端模板异常'},500);return
            content=template.replace('__CQ_TOKEN__',json.dumps(self.server.token))
            self._headers(200,'text/html; charset=utf-8');self.end_headers();self.wfile.write(content.encode('utf-8'));return
        if not self._authorized():return
        try:
            if parsed.path=='/api/stocks':
                from alerts.signal_engine import RULE_LABELS
                self._json({'stocks':self.server.universe,'rules':RULE_LABELS});return
            if parsed.path=='/api/status':
                self._json(self.server.monitor.status());return
            if parsed.path=='/api/alerts':
                query=urllib.parse.parse_qs(parsed.query)
                after=max(0,int(query.get('after',['0'])[0]));limit=max(1,min(500,int(query.get('limit',['200'])[0])))
                # Take the initial history watermark before reading. Normal
                # polling advances only through rows actually returned, so a
                # concurrent insert or a full page cannot skip a notification.
                initial=query.get('recent',['0'])[0]=='1'
                watermark=self.server.store.max_id() if initial else after
                if initial:after=max(0,watermark-limit)
                rows=self.server.store.events_after(after,limit)
                cursor=max([watermark]+[int(row['id']) for row in rows])
                self._json({'alerts':rows,'last_id':cursor});return
            if parsed.path=='/api/directories':
                choices=[Path(r'D:\tdx\vipdoc'),Path(r'D:\new_tdx\vipdoc'),Path(r'C:\new_tdx\vipdoc')]
                self._json({'directories':[str(p) for p in choices if p.is_absolute() and p.is_dir()]});return
            if parsed.path=='/api/export-alerts.csv':
                self._export();return
            self._json({'ok':False,'error':'接口不存在'},404)
        except (ValueError,TypeError,OSError) as exc:
            self._json({'ok':False,'error':str(exc)},400)
        except Exception:
            self._json({'ok':False,'error':'读取失败，请查看页面数据状态并重试'},500)

    def _export(self):
        stream=io.StringIO(newline='');fields=['id','created_at','symbol','rule','bar_end','origin_time','message','is_test','evidence']
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        after=0;written=0
        while written<100000:
            rows=self.server.store.events_after(after,500)
            if not rows:break
            for row in rows:
                clean={k:row.get(k,'') for k in fields}
                clean['evidence']=json.dumps(clean['evidence'],ensure_ascii=False,default=str)
                for key,value in clean.items():
                    if isinstance(value,str) and value.startswith(('=','+','-','@')):clean[key]="'"+value
                writer.writerow(clean);written+=1
            after=max(int(r['id']) for r in rows)
        self._headers(200,'text/csv; charset=utf-8')
        self.send_header('Content-Disposition','attachment; filename="chanquant-alerts.csv"');self.end_headers()
        self.wfile.write(stream.getvalue().encode('utf-8-sig'))

    def do_POST(self):
        if not self._authorized():return
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<=length<=1048576:raise ValueError('请求过大')
            data=json.loads(self.rfile.read(length) or b'{}')
            if not isinstance(data,dict):raise ValueError('请求应为对象')
            path=urllib.parse.urlparse(self.path).path
            if path=='/api/config':
                config=self.server.monitor.configure(data);self._json({'ok':True,'config':config});return
            if path=='/api/start':
                self.server.monitor.start();self._json({'ok':True});return
            if path=='/api/stop':
                self.server.monitor.pause();self._json({'ok':True});return
            if path=='/api/test-alert':
                self._json({'ok':True,'alert':self.server.monitor.test_alert()});return
            if path=='/api/check-market':
                symbol=data.get('symbol')
                if not set(data)<={'symbol','data_source','tdx_host','tdx_port'} or not isinstance(symbol,str) or symbol not in {s['symbol'] for s in self.server.universe}:
                    raise ValueError('请选择股票池内的个股检查行情连接')
                config=self.server.monitor.status()['config']
                source=data.get('data_source',config['data_source'])
                if 'data_source' not in data and source=='tdx_local':source='tdx_online'
                host=data.get('tdx_host',config.get('tdx_host',''))
                port=data.get('tdx_port',config.get('tdx_port',7709))
                if source not in {'tdx_online','eastmoney'}:raise ValueError('请选择通达信行情直连或东方财富联网行情')
                from alerts.tdx_online import validate_endpoint
                host,port=validate_endpoint(host,port)
                self._json({'ok':True,**check_online_market(symbol,data_source=source,host=host,port=port)});return
            if path=='/api/shutdown':
                self.server.monitor.pause();self._json({'ok':True})
                threading.Thread(target=self.server.shutdown,daemon=True).start();return
            self._json({'ok':False,'error':'接口不存在'},404)
        except (ValueError,TypeError,OSError) as exc:
            self._json({'ok':False,'error':str(exc)},400)
        except Exception:
            self._json({'ok':False,'error':'操作失败，请检查配置与本地数据'},500)


def self_check():
    import importlib.metadata
    from alerts.sources import read_lc5,discover_lc5
    from alerts.signal_engine import RULE_LABELS
    from alerts.monitor import AlertStore,LocalMonitor
    from czsc import CZSC,BarGenerator,CzscSignals,format_standard_kline
    from alerts.online import fetch_five_minute
    from pytdx.hq import TdxHq_API
    from alerts.tdx_online import fetch_five_minute as fetch_tdx
    version=importlib.metadata.version('czsc')
    return {'app':APP_ID,'version':APP_VERSION,'stocks':len(load_universe()),'czsc_version':version,'rules':RULE_LABELS,
            'pytdx_version':importlib.metadata.version('pytdx'),
            'sources':{'tdx_online':'通达信行情直连','tdx_local':'通达信本地5分钟文件','eastmoney':'东方财富公开5分钟行情'},
            'mode':'read-only alerts; no broker or order routing'}


def _china_now():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo('Asia/Shanghai'))


def check_online_market(symbol,now=None,*,data_source='tdx_online',host='',port=7709):
    """Fetch actual source data without persisting or notifying any signal."""
    from alerts.online import fetch_five_minute
    from alerts.tdx_online import fetch_five_minute as fetch_tdx,validate_endpoint
    from alerts.signal_engine import analyze_five_minute
    from alerts.monitor import latest_completed_bar,_local_stamp
    if symbol not in {stock['symbol'] for stock in load_universe()}:
        raise ValueError('请选择股票池内的个股检查行情连接')
    if data_source not in {'tdx_online','eastmoney'}:
        raise ValueError('请选择通达信行情直连或东方财富联网行情')
    host,port=validate_endpoint(host,port)
    current=_local_stamp(now) if now is not None else _china_now()
    frame=fetch_tdx(symbol,now=current,host=host,port=port) if data_source=='tdx_online' else fetch_five_minute(symbol,now=current)
    analysis_now=current if now is not None else _china_now()
    result=analyze_five_minute(frame,warmup=200,now=analysis_now)
    final_now=current if now is not None else _china_now()
    expected=latest_completed_bar(final_now)
    asof=_local_stamp(result['asof']) if result.get('asof') else None
    return {key:result.get(key) for key in ('symbol','asof','last_five_asof','bars30_count','status','snapshot','quality','source')}|{'fresh':asof is not None and asof==expected,'required_bars30':201}


def main(argv=None):
    parser=argparse.ArgumentParser(description='ChanQuant 本地个股预警助手')
    parser.add_argument('--port',type=int,default=17343)
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--data-dir',type=Path,default=ROOT/'app_data')
    parser.add_argument('--self-check',action='store_true')
    parser.add_argument('--check-online',metavar='SYMBOL',help='只读检查联网行情，例如600660.SH，不创建预警')
    parser.add_argument('--source',choices=('tdx_online','eastmoney'),default='tdx_online',help='联网检查来源，默认通达信行情直连')
    parser.add_argument('--tdx-host',default='',help='通达信行情主站IP或域名，留空使用自动节点')
    parser.add_argument('--tdx-port',type=int,default=7709)
    args=parser.parse_args(argv)
    if args.self_check:print(json.dumps(self_check(),ensure_ascii=False,indent=2));return 0
    if args.check_online:
        try:
            print(json.dumps({'ok':True,**check_online_market(args.check_online,data_source=args.source,host=args.tdx_host,port=args.tdx_port)},ensure_ascii=False,indent=2,default=str))
            return 0
        except (OSError,ValueError,RuntimeError) as exc:
            print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=False))
            return 1
    from alerts.monitor import AlertStore,LocalMonitor
    universe=load_universe();args.data_dir.mkdir(parents=True,exist_ok=True)
    store=AlertStore(args.data_dir/'alerts.sqlite3');monitor=LocalMonitor(universe,store)
    try:
        server=AlertHTTPServer(('127.0.0.1',args.port),monitor,store,universe)
    except OSError:
        monitor.close()
        if args.port:
            url=f'http://127.0.0.1:{args.port}'
            try:
                response=json.loads(urllib.request.urlopen(url+'/api/health',timeout=2).read())
                if response.get('app')==APP_ID:
                    if response.get('version')!=APP_VERSION:
                        raise SystemExit('旧版预警助手仍在运行。请先在原页面点击“退出程序”并关闭原启动窗口，再启动新版。')
                    if not args.no_browser:webbrowser.open(url)
                    print('预警助手已在运行，已打开原窗口。');return 0
            except (OSError,ValueError):pass
        raise SystemExit('本地端口被占用。请关闭原程序，或使用 --port 指定其他端口；未终止任何已有进程。')
    url=f'http://127.0.0.1:{server.server_address[1]}'
    print('ChanQuant个股预警助手已启动；仅本机只读预警，无交易功能。',flush=True)
    print('请保持此窗口运行；按Ctrl+C或页面“退出程序”结束。',flush=True)
    if not args.no_browser:webbrowser.open(url)
    try:server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:pass
    finally:monitor.close();server.server_close()
    return 0


if __name__=='__main__':raise SystemExit(main())
