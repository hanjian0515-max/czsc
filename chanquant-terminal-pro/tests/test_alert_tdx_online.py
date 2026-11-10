"""Synthetic protocol fixtures only; no external TDX socket is opened."""
from copy import deepcopy
from datetime import datetime
import inspect
import math
from types import SimpleNamespace

import pandas as pd
import pytest

from alerts import tdx_online as source


def row(dt='2026-11-10 09:35', **extra):
    value={'datetime':dt,'open':10.0,'close':10.1,'high':10.3,'low':9.9,
           'vol':123.0,'amount':1234.5}
    value.update(extra)
    return value


def history(count=3200, **extra):
    stamps=[day+pd.Timedelta(minutes=minute)
        for day in pd.bdate_range('2026-01-01',periods=70)
        for minute in sorted(source.END5)]
    return [row(stamp.strftime('%Y-%m-%d %H:%M'),**extra) for stamp in stamps[-count:]]


class FakeClient:
    def __init__(self, rows=None, pages=None, connect=True, disconnect_error=None):
        self.rows=[row()] if rows is None else rows
        self.pages=pages
        self.connect_result=connect
        self.disconnect_error=disconnect_error
        self.calls=[]
        self.closed=0
        self.client=SimpleNamespace(close=self._close_socket)

    def _close_socket(self):self.closed+=1

    def connect(self,host,port,time_out):
        self.endpoint=(host,port)
        self.timeout=time_out
        if isinstance(self.connect_result,Exception):raise self.connect_result
        return self if self.connect_result else False

    def get_security_bars(self,category,market,code,start,count):
        self.calls.append((category,market,code,start,count))
        if self.pages is not None:
            result=self.pages.get(start,[])
        else:
            end=len(self.rows)-start
            result=self.rows[max(0,end-count):end] if end>0 else []
        if isinstance(result,Exception):raise result
        return deepcopy(result)

    def disconnect(self):
        self.disconnected=True
        if self.disconnect_error:raise self.disconnect_error


class Factory:
    def __init__(self,*clients):self.clients=list(clients);self.kwargs=[];self.used=[]
    def __call__(self,**kwargs):
        self.kwargs.append(kwargs)
        client=self.clients.pop(0)
        self.used.append(client)
        return client


@pytest.fixture(autouse=True)
def clear_state():
    source.reset_connection_state()
    yield
    source.reset_connection_state()


def fetch(client,**kwargs):
    return source.fetch_five_minute('600660.SH',host='127.0.0.1',
        client_factory=Factory(client),now='2026-11-10 10:01',**kwargs)


def test_real_pytdx_api_signature_and_selected_hosts_are_compatible():
    from pytdx.hq import TdxHq_API
    from pytdx.config.hosts import hq_hosts
    from pytdx.params import TDXParams
    assert TDXParams.KLINE_TYPE_5MIN==0
    assert all(name in inspect.signature(TdxHq_API).parameters for name in ('heartbeat','auto_retry','raise_exception'))
    assert 'time_out' in inspect.signature(TdxHq_API.connect).parameters
    assert tuple(inspect.signature(TdxHq_API.get_security_bars).parameters)==('self','category','market','code','start','count')
    assert 4<=len(source._default_servers())<=6
    assert all(server in hq_hosts for server in source._default_servers())


@pytest.mark.parametrize('host,port,expected',[
    ('',7709,('',7709)),(' 127.0.0.1 ',80,('127.0.0.1',80)),
    ('QUOTE.Example.com',7709,('quote.example.com',7709)),('localhost',7709,('localhost',7709)),
    (' quote.example.com ',7709,('quote.example.com',7709))])
def test_endpoint_normalization(host,port,expected):
    assert source.validate_endpoint(host,port)==expected


@pytest.mark.parametrize('host',[None,1,[],True,'http://127.0.0.1','tdx://server','host/path',
    'host:7709','user@host','host name','host\nname','host;command','-bad.com','bad-.com',
    'bad..com','a'*64+'.com','a'*254,'999.1.1.1','127.0.0','::1'])
def test_bad_host_never_constructs_client(host):
    with pytest.raises(source.TdxDataError):
        source.fetch_five_minute('600660.SH',host=host,client_factory=lambda **kw:pytest.fail('no socket'))


@pytest.mark.parametrize('port',[True,False,0,-1,65536,7709.0,'7709',None])
def test_bad_port_never_constructs_client(port):
    with pytest.raises(source.TdxDataError):
        source.fetch_five_minute('600660.SH',port=port,client_factory=lambda **kw:pytest.fail('no socket'))


@pytest.mark.parametrize('symbol',[None,600660,'600660','600660.BJ','600660.sh','60066.SH','600660.SH/x'])
def test_bad_symbol_before_network(symbol):
    with pytest.raises(source.TdxDataError):
        source.fetch_five_minute(symbol,client_factory=lambda **kw:pytest.fail('no socket'))


def test_four_pages_use_category_zero_same_server_and_original_volume():
    data=history()
    original=deepcopy(data)
    client=FakeClient(rows=data)
    factory=Factory(client)
    frame=source.fetch_five_minute('600660.SH',host='127.0.0.1',port=7709,now='2026-11-10 10:01',client_factory=factory)
    assert list(frame.columns)==source.FIELDS and len(frame)==3200
    assert frame.dt.is_monotonic_increasing and frame.symbol.eq('600660.SH').all()
    assert frame.vol.eq(123).all() and frame.amount.eq(1234.5).all()
    assert client.calls==[(0,1,'600660',start,800) for start in (0,800,1600,2400)]
    assert client.timeout==2 and client.endpoint==('127.0.0.1',7709)
    assert factory.kwargs==[{'heartbeat':False,'auto_retry':False,'raise_exception':True}]
    assert client.disconnected and client.closed==1
    assert data==original
    attrs=frame.attrs
    assert attrs['provider']=='tdx' and attrs['source_format']=='TDX-HQ-5M'
    assert attrs['source_host']=='127.0.0.1' and attrs['source_port']==7709
    assert attrs['source_url']=='tdx://127.0.0.1:7709'
    assert attrs['price_basis']=='raw' and attrs['loaded_records']==attrs['history_target']==3200
    assert attrs['retrieved_at']=='2026-11-10T10:01:00+08:00'
    assert len(attrs['response_sha256'])==64


def test_shenzhen_market_and_padded_response_code():
    client=FakeClient(rows=[row(code=333,market=0)])
    result=source.fetch_five_minute('000333.SZ',host='localhost',client_factory=Factory(client))
    assert result.symbol.iloc[0]=='000333.SZ' and client.calls[0][1:3]==(0,'000333')


def test_short_first_page_stays_short_and_does_not_request_older():
    client=FakeClient(rows=history(20))
    frame=fetch(client)
    assert len(frame)==20 and len(client.calls)==1
    assert frame.attrs['history_target']==3200


def test_auto_mode_honors_port_and_failure_cooldown_is_scoped_by_port(monkeypatch):
    monkeypatch.setattr(source,'_default_servers',lambda:[('synthetic endpoint','192.0.2.1',7709)])
    with pytest.raises(source.TdxNetworkError):
        source.fetch_five_minute('600660.SH',port=7709,client_factory=Factory(FakeClient(connect=False)))
    client=FakeClient()
    result=source.fetch_five_minute('600660.SH',port=7710,client_factory=Factory(client))
    assert client.endpoint==('192.0.2.1',7710)
    assert result.attrs['source_port']==7710


def test_empty_older_page_after_full_page_means_history_exhausted():
    client=FakeClient(rows=history(800))
    frame=fetch(client)
    assert len(frame)==800 and len(client.calls)==2


@pytest.mark.parametrize('rows',[[],None,{},'invalid',tuple([row()]),[row()]*801])
def test_invalid_or_empty_first_page_is_not_replaced(rows):
    client=FakeClient(pages={0:rows})
    with pytest.raises(source.TdxDataError):fetch(client)
    assert len(client.calls)==1 and client.disconnected and client.closed==1


@pytest.mark.parametrize('field',['open','close','high','low','vol','amount'])
@pytest.mark.parametrize('bad',[None,True,'-',float('nan'),float('inf'),{},'__import__("os")'])
def test_invalid_required_numeric_fields(field,bad):
    with pytest.raises(source.TdxDataError):fetch(FakeClient(rows=[row(**{field:bad})]))


@pytest.mark.parametrize('change',[{'open':0},{'close':-1},{'low':-1},{'high':0},
    {'vol':-1},{'amount':-1},{'high':10.05},{'low':10.2}])
def test_invalid_ohlc_or_negative_quantity(change):
    with pytest.raises(source.TdxDataError):fetch(FakeClient(rows=[row(**change)]))


@pytest.mark.parametrize('dt',['2026-11-10 09:30','2026-11-10 12:00','2026-11-10 09:36',
    '2026-11-10 09:35:00','2026-11-10T09:35','2026-11-10 15:05','2026-11-08 09:35',
    '2026-02-30 09:35','bad',''])
def test_invalid_source_timestamps(dt):
    with pytest.raises(source.TdxDataError):fetch(FakeClient(rows=[row(dt)]))


def test_missing_timestamp_and_required_field_rejected():
    value=row();del value['datetime']
    with pytest.raises(source.TdxDataError):fetch(FakeClient(rows=[value]))
    value=row();del value['amount']
    with pytest.raises(source.TdxDataError):fetch(FakeClient(rows=[value]))


@pytest.mark.parametrize('metadata',[{'code':'600000'},{'code':True},{'market':0},{'market':True},
    {'year':2025},{'minute':40},{'hour':True}])
def test_optional_response_identity_and_date_fields_cannot_conflict(metadata):
    with pytest.raises(source.TdxDataError):fetch(FakeClient(rows=[row(**metadata)]))


@pytest.mark.parametrize('order',['duplicate','reverse'])
def test_page_order_is_not_sorted_or_deduplicated(order):
    values=[row('2026-11-10 09:35'),row('2026-11-10 09:40')]
    values=[values[0],values[0]] if order=='duplicate' else list(reversed(values))
    with pytest.raises(source.TdxDataError,match='重复或倒序'):fetch(FakeClient(rows=values))


def test_overlapping_pages_are_not_silently_deduplicated():
    first=history(800)
    client=FakeClient(pages={0:first,800:[deepcopy(first[0])]})
    with pytest.raises(source.TdxDataError,match='分页时间重叠'):fetch(client)


def test_wrong_page_direction_rejected():
    first=history(800)
    client=FakeClient(pages={0:first,800:[deepcopy(first[-1])]})
    with pytest.raises(source.TdxDataError,match='分页时间重叠'):fetch(client)


def test_future_end_bars_are_preserved_for_engine_clock_filtering():
    result=fetch(FakeClient(rows=[row('2026-11-10 15:00')]))
    assert result.dt.iloc[0]==pd.Timestamp('2026-11-10 15:00')


def test_transport_failure_discards_partial_page_before_trying_next_node(monkeypatch):
    servers=[('A','127.0.0.1',7709),('B','127.0.0.2',7709)]
    monkeypatch.setattr(source,'_default_servers',lambda:servers)
    first=FakeClient(pages={0:history(800,vol=1),800:TimeoutError('synthetic')})
    second=FakeClient(rows=history(1000,vol=99))
    result=source.fetch_five_minute('600660.SH',client_factory=Factory(first,second))
    assert len(result)==1000 and result.vol.eq(99).all()
    assert result.attrs['source_host']=='127.0.0.2'
    assert first.calls[-1][3]==800 and second.calls[0][3]==0
    assert first.disconnected and second.disconnected


def test_data_error_never_switches_node(monkeypatch):
    monkeypatch.setattr(source,'_default_servers',lambda:[('A','127.0.0.1',7709),('B','127.0.0.2',7709)])
    factory=Factory(FakeClient(rows=[row(high=1)]),FakeClient())
    with pytest.raises(source.TdxDataError):source.fetch_five_minute('600660.SH',client_factory=factory)
    assert len(factory.used)==1


def test_manual_endpoint_never_switches_to_auto_servers(monkeypatch):
    monkeypatch.setattr(source,'_default_servers',lambda:pytest.fail('manual mode must not use auto nodes'))
    factory=Factory(FakeClient(connect=False),FakeClient())
    with pytest.raises(source.TdxNetworkError):
        source.fetch_five_minute('600660.SH',host='localhost',client_factory=factory)
    assert len(factory.used)==1


def test_actual_pytdx_wrapped_socket_error_is_transport_failure(monkeypatch):
    from pytdx.errors import TdxFunctionCallError
    wrapped=TdxFunctionCallError('synthetic')
    wrapped.original_exception=TimeoutError('synthetic socket timeout')
    monkeypatch.setattr(source,'_default_servers',lambda:[('A','127.0.0.1',7709),('B','127.0.0.2',7709)])
    factory=Factory(FakeClient(pages={0:wrapped}),FakeClient())
    assert len(source.fetch_five_minute('600660.SH',client_factory=factory))==1
    assert len(factory.used)==2


def test_actual_pytdx_parser_error_is_data_error_and_not_retried(monkeypatch):
    from pytdx.errors import TdxFunctionCallError
    wrapped=TdxFunctionCallError('synthetic')
    wrapped.original_exception=ValueError('synthetic corrupt response')
    monkeypatch.setattr(source,'_default_servers',lambda:[('A','127.0.0.1',7709),('B','127.0.0.2',7709)])
    factory=Factory(FakeClient(pages={0:wrapped}),FakeClient())
    with pytest.raises(source.TdxDataError):source.fetch_five_minute('600660.SH',client_factory=factory)
    assert len(factory.used)==1


def test_connection_failure_disconnects_even_if_disconnect_raises():
    client=FakeClient(connect=False,disconnect_error=RuntimeError('synthetic shutdown failed'))
    with pytest.raises(source.TdxNetworkError):fetch(client)
    assert client.disconnected and client.closed==1


def test_no_price_cache_and_response_hash_tracks_actual_normalized_rows():
    first=fetch(FakeClient(rows=[row()]))
    second=fetch(FakeClient(rows=[row(close=10.2)]))
    assert second.close.iloc[0]==10.2 and first.close.iloc[0]==10.1
    assert first.attrs['response_sha256']!=second.attrs['response_sha256']


def test_all_failed_nodes_use_cooldown_and_then_can_recover(monkeypatch):
    clock={'now':0.}
    monkeypatch.setattr(source.time,'monotonic',lambda:clock['now'])
    monkeypatch.setattr(source,'_default_servers',lambda:[('A','127.0.0.1',7709),('B','127.0.0.2',7709)])
    factory=Factory(FakeClient(connect=False),FakeClient(connect=False),FakeClient())
    with pytest.raises(source.TdxNetworkError):source.fetch_five_minute('600660.SH',client_factory=factory)
    with pytest.raises(source.TdxNetworkError,match='30秒'):
        source.fetch_five_minute('000333.SZ',client_factory=factory)
    assert len(factory.used)==2
    clock['now']=31
    assert len(source.fetch_five_minute('600660.SH',client_factory=factory))==1


def test_changed_manual_host_is_not_blocked_by_other_endpoint_cooldown():
    factory=Factory(FakeClient(connect=False),FakeClient())
    with pytest.raises(source.TdxNetworkError):
        source.fetch_five_minute('600660.SH',host='127.0.0.1',client_factory=factory)
    assert len(source.fetch_five_minute('600660.SH',host='127.0.0.2',client_factory=factory))==1


def test_successful_auto_node_is_preferred_on_next_request(monkeypatch):
    monkeypatch.setattr(source,'_default_servers',lambda:[('A','127.0.0.1',7709),('B','127.0.0.2',7709)])
    first,second,third=FakeClient(connect=False),FakeClient(),FakeClient()
    source.fetch_five_minute('600660.SH',client_factory=Factory(first,second))
    source.fetch_five_minute('000333.SZ',client_factory=Factory(third))
    assert second.endpoint==third.endpoint==('127.0.0.2',7709)


def test_endpoint_metadata_cache_is_bounded_and_never_stores_quotes():
    for index in range(40):
        with pytest.raises(source.TdxNetworkError):
            source.fetch_five_minute('600660.SH',host=f'127.0.1.{index}',client_factory=Factory(FakeClient(connect=False)))
    assert len(source._STATES)==source.MAX_CONFIG_STATES
    assert all(set(state)<={'until','last_good'} for state in source._STATES.values())


def test_deadline_stops_before_another_page_or_node(monkeypatch):
    clock={'now':0.}
    monkeypatch.setattr(source.time,'monotonic',lambda:clock['now'])
    monkeypatch.setattr(source,'_default_servers',lambda:[('A','127.0.0.1',7709),('B','127.0.0.2',7709)])
    class SlowClient(FakeClient):
        def get_security_bars(self,*args):
            result=super().get_security_bars(*args)
            clock['now']=source.REQUEST_BUDGET_SECONDS+1
            return result
    first=SlowClient(rows=history(800))
    factory=Factory(first,FakeClient())
    with pytest.raises(source.TdxNetworkError):source.fetch_five_minute('600660.SH',client_factory=factory)
    assert len(first.calls)==1 and len(factory.used)==1


@pytest.mark.parametrize('now',['bad',pd.NaT,float('nan')])
def test_bad_retrieval_clock_prevents_network(now):
    with pytest.raises(source.TdxDataError):
        source.fetch_five_minute('600660.SH',now=now,client_factory=lambda **kw:pytest.fail('no socket'))


def test_timezone_conversion_and_client_factory_validation():
    result=source.fetch_five_minute('600660.SH',now='2026-11-10T02:00:00Z',host='localhost',client_factory=Factory(FakeClient()))
    assert result.attrs['retrieved_at']=='2026-11-10T10:00:00+08:00'
    with pytest.raises(source.TdxDataError):source.fetch_five_minute('600660.SH',client_factory=1)
