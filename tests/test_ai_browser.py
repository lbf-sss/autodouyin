from unittest.mock import Mock
import pytest
from douyinfire.browser import DouyinDOMAdapter, DouyinBrowser, BrowserError, SendUncertain, UnsupportedPage


def row(**updates):
    value = dict(server_id='123', client_id='c1', server_status=0, from_me=False, sender='friend',
                 conversation_id='conversation1', conversation_type=1, created_at=1000, message_type=7, text='你好')
    value.update(updates)
    return value


def adapter(monkeypatch, rows):
    a = DouyinDOMAdapter(expected_account_id='own')
    monkeypatch.setattr(a, 'conversation_identity', lambda p: 'friend')
    monkeypatch.setattr(a, '_rendered_account', lambda p: {'identity':'own','name':'我'})
    monkeypatch.setattr(a, '_messages', lambda p: rows)
    page = Mock()
    page.locator.return_value.count.return_value = 1
    header = page.locator.return_value.nth.return_value
    header.is_visible.return_value = True
    header.evaluate.return_value = {'type':1,'id':'conversation1','direct_id':'friend'}
    return a, page


def test_history_is_bound_to_account_conversation_and_sender(monkeypatch):
    a, page = adapter(monkeypatch, [row(),row(server_id='124',from_me=True,sender='own')])
    assert len(a.read_direct_history(page,'friend')) == 2


@pytest.mark.parametrize('update', [{'conversation_id':'other'}, {'sender':'other'}, {'message_type':None}, {'server_id':'0'}, {'server_status':1}])
def test_history_rejects_unverified_or_pending_rows(monkeypatch, update):
    a,page = adapter(monkeypatch,[row(**update)])
    with pytest.raises((BrowserError, UnsupportedPage)):
        a.read_direct_history(page,'friend')


def test_media_sender_must_match_verified_contact(monkeypatch):
    a,page=adapter(monkeypatch,[row(message_type=8,sender='another-person',media={'kind':'video'})])
    with pytest.raises(UnsupportedPage):
        a.read_direct_history(page,'friend')


def browser(tmp_path):
    b = DouyinBrowser(tmp_path/'p',tmp_path/'s')
    b.page = Mock()
    b.adapter = Mock()
    b.adapter.message_snapshot.return_value = {'122'}
    b._check_conversation = Mock()
    b._check_navigation = Mock()
    b._check_login_gate = Mock()
    b._poll = Mock(return_value={'status':'confirmed','message_id':'123'})
    return b


def test_stale_reply_guard_cancels_before_durable_or_send(tmp_path):
    b = browser(tmp_path)
    before = Mock()
    def stale(): raise BrowserError('消息已变化')
    with pytest.raises(BrowserError):
        b.send_current_reply('friend','你好',before_send=before,guard=stale)
    before.assert_not_called()
    b.adapter.trigger_send.assert_not_called()
    b.adapter.discard_prepared.assert_called_once()


def test_reply_durable_boundary_is_uncertain_even_if_callback_raises(tmp_path):
    b = browser(tmp_path)
    def fail(): raise RuntimeError('disk')
    with pytest.raises(SendUncertain):
        b.send_current_reply('friend','你好',before_send=fail,guard=lambda:None)
    b.adapter.trigger_send.assert_not_called()


def test_reply_checks_guard_before_submit(tmp_path):
    b = browser(tmp_path)
    events=[]
    b.adapter.trigger_send.side_effect=lambda page,**kwargs:events.append('send')
    b.send_current_reply('friend','你好',before_send=lambda:events.append('durable'),guard=lambda:events.append('guard'))
    assert events == ['guard','durable','guard','send']


def test_guard_rechecked_after_slow_durable_boundary(tmp_path):
    b = browser(tmp_path)
    changed = [False]
    def guard():
        if changed[0]: raise BrowserError('时间段已结束')
    def before(): changed[0] = True
    with pytest.raises(SendUncertain):
        b.send_current_reply('friend','你好',before_send=before,guard=guard)
    b.adapter.trigger_send.assert_not_called()
