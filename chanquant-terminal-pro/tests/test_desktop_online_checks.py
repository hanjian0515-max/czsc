"""Offline CLI/probe contracts; all price/result fixtures are synthetic."""
from datetime import datetime
import io
import json
from zoneinfo import ZoneInfo

import pytest

import desktop_alerts as app
from alerts import online,signal_engine

CHINA=ZoneInfo('Asia/Shanghai')


def stamp(text):
    return datetime.fromisoformat(text).replace(tzinfo=CHINA)


def synthetic_analysis():
    return {'symbol':'600660.SH','asof':'2026-11-10T10:00:00',
            'last_five_asof':'2026-11-10T10:00:00','bars30_count':201,
            'status':'ready','snapshot':{'close':10.0},'quality':{'alertable':True},
            'source':{'kind':'synthetic_test_fixture','label':'not market data'},
            'events':[{'rule':'buy2','message':'synthetic event, never delivered by probe'}]}


def test_probe_rechecks_real_clock_after_slow_fetch_and_analysis(monkeypatch):
    moments=iter(map(stamp,['2026-11-10T10:29:59','2026-11-10T10:30:02','2026-11-10T10:30:04']))
    monkeypatch.setattr(app,'_china_now',lambda:next(moments))
    seen={}
    frame=object()
    def fetch(symbol,*,now):
        seen['fetch_now']=now
        return frame
    def analyze(value,*,warmup,now):
        assert value is frame and warmup==200
        seen['analysis_now']=now
        return synthetic_analysis()
    monkeypatch.setattr(online,'fetch_five_minute',fetch)
    monkeypatch.setattr(signal_engine,'analyze_five_minute',analyze)
    result=app.check_online_market('600660.SH')
    assert seen['fetch_now']==stamp('2026-11-10T10:29:59')
    assert seen['analysis_now']==stamp('2026-11-10T10:30:02')
    assert result['fresh'] is False
    assert 'events' not in result


def test_explicit_probe_clock_is_deterministic_and_does_not_return_alerts(monkeypatch):
    monkeypatch.setattr(app,'_china_now',lambda:pytest.fail('injected clock must remain fixed'))
    monkeypatch.setattr(online,'fetch_five_minute',lambda symbol,*,now:object())
    monkeypatch.setattr(signal_engine,'analyze_five_minute',lambda frame,**kwargs:synthetic_analysis())
    result=app.check_online_market('600660.SH',now=stamp('2026-11-10T10:15:00'))
    assert result['fresh'] is True
    assert result['source']['kind']=='synthetic_test_fixture'
    assert 'events' not in result


@pytest.mark.parametrize('symbol',['000001.SH','515880.SH','600660','600660.BJ','999999.SH'])
def test_probe_rejects_non_pool_symbol_before_network(monkeypatch,symbol):
    monkeypatch.setattr(online,'fetch_five_minute',lambda *args,**kwargs:pytest.fail('no network for invalid stock'))
    with pytest.raises(ValueError,match='股票池'):
        app.check_online_market(symbol)


def test_network_failure_never_runs_signal_analysis(monkeypatch):
    calls=[]
    def fail(*args,**kwargs):
        calls.append(1)
        raise online.OnlineNetworkError('synthetic transport failure')
    monkeypatch.setattr(online,'fetch_five_minute',fail)
    monkeypatch.setattr(signal_engine,'analyze_five_minute',lambda *args,**kwargs:pytest.fail('no analysis fallback'))
    with pytest.raises(online.OnlineNetworkError):
        app.check_online_market('600660.SH')
    assert len(calls)==1


@pytest.mark.parametrize('failure',[False,True])
def test_cli_check_outputs_json_without_starting_server(monkeypatch,capsys,failure):
    def check(symbol):
        assert symbol=='600660.SH'
        if failure:raise online.OnlineNetworkError('synthetic failure')
        return {**synthetic_analysis(),'fresh':True}
    monkeypatch.setattr(app,'check_online_market',check)
    monkeypatch.setattr(app,'AlertHTTPServer',lambda *args,**kwargs:pytest.fail('probe must not start monitor'))
    assert app.main(['--check-online','600660.SH'])==(1 if failure else 0)
    output=json.loads(capsys.readouterr().out)
    assert output['ok'] is not failure


@pytest.mark.parametrize('version',[None,'1.0','0.9.0'])
def test_upgrade_does_not_silently_reopen_older_server(monkeypatch,tmp_path,version):
    def occupied(*args,**kwargs):raise OSError('synthetic port occupied')
    monkeypatch.setattr(app,'AlertHTTPServer',occupied)
    payload={'app':app.APP_ID,'version':version}
    monkeypatch.setattr(app.urllib.request,'urlopen',lambda *args,**kwargs:io.BytesIO(json.dumps(payload).encode()))
    monkeypatch.setattr(app.webbrowser,'open',lambda *args:pytest.fail('must explain old version first'))
    with pytest.raises(SystemExit,match='旧版预警助手仍在运行'):
        app.main(['--data-dir',str(tmp_path)])


def test_current_server_reuses_existing_instance(monkeypatch,tmp_path):
    def occupied(*args,**kwargs):raise OSError('synthetic port occupied')
    monkeypatch.setattr(app,'AlertHTTPServer',occupied)
    payload={'app':app.APP_ID,'version':app.APP_VERSION}
    monkeypatch.setattr(app.urllib.request,'urlopen',lambda *args,**kwargs:io.BytesIO(json.dumps(payload).encode()))
    assert app.main(['--no-browser','--data-dir',str(tmp_path)])==0
