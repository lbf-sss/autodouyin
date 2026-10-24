from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from douyinfire import ai_core
from douyinfire.config import AIReplyConfig, ConfigError, config_to_dict, parse_config


def configured(**values):
    return AIReplyConfig(enabled=True, user_id='account', contact_ids=['friend'], generation='activation', **values)


def test_config_old_files_default_to_disabled_and_round_trip():
    cfg = parse_config({'users': []})
    assert cfg.ai_reply == AIReplyConfig()
    assert config_to_dict(cfg)['ai_reply']['enabled'] is False


def raw_config(**values):
    return {'users': [{'id': 'account', 'name': 'main', 'identity': 'self-secUid',
                      'contacts': [{'id': 'friend', 'name': 'friend', 'identity': 'friend-secUid'}]}],
            'ai_reply': {'enabled': True, 'user_id': 'account', 'contact_ids': ['friend'],
                         'generation': 'activation', **values}}


@pytest.mark.parametrize('values', [
    {'enabled': 'true'}, {'start': '7:00'}, {'end': '24:00'}, {'start': '21:00', 'end': '21:00'},
    {'timezone': 'Mars/Nowhere'}, {'generation': ''}, {'generation': 2}, {'user_id': 'missing'},
    {'contact_ids': ['friend', 'friend']}, {'contact_ids': ['missing']}, {'contact_ids': []},
    {'contact_ids': 'friend'}, {'contact_ids': [1]},
])
def test_config_rejects_invalid_ai_settings(values):
    with pytest.raises(ConfigError):
        parse_config(raw_config(**values))


@pytest.mark.parametrize('mutate', [
    lambda raw: raw['users'][0].update(enabled=False),
    lambda raw: raw['users'][0].update(identity=''),
    lambda raw: raw['users'][0]['contacts'][0].update(enabled=False),
    lambda raw: raw['users'][0]['contacts'][0].update(identity=''),
    lambda raw: raw['users'][0]['contacts'][0].update(kind='group', identity=''),
])
def test_enabled_ai_requires_bound_enabled_direct_contact(mutate):
    raw = raw_config()
    mutate(raw)
    with pytest.raises(ConfigError):
        parse_config(raw)


def test_disabled_ai_tolerates_stale_bindings():
    cfg = parse_config({'users': [], 'ai_reply': {'user_id': 'gone', 'contact_ids': ['old']}})
    assert cfg.ai_reply.user_id == 'gone'
    cfg = parse_config(raw_config())
    assert cfg.ai_reply.user_id == 'account'
    assert parse_config(config_to_dict(cfg)).ai_reply == cfg.ai_reply


def test_window_is_shanghai_inclusive_start_exclusive_end():
    settings = configured()
    assert not ai_core.in_window(settings, datetime(2026, 10, 20, 10, 59, 59, tzinfo=timezone.utc))
    assert ai_core.in_window(settings, datetime(2026, 10, 20, 11, 0, tzinfo=timezone.utc))
    assert ai_core.in_window(settings, datetime(2026, 10, 20, 12, 59, 59, tzinfo=timezone.utc))
    assert not ai_core.in_window(settings, datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc))
    assert ai_core.window_key(settings, datetime(2026, 10, 20, 11, 0, tzinfo=timezone.utc)) != ai_core.window_key(settings, datetime(2026, 10, 21, 11, 0, tzinfo=timezone.utc))
    assert not ai_core.in_window(AIReplyConfig(), datetime(2026, 10, 20, 11, 0, tzinfo=timezone.utc))


def test_overnight_window_has_one_occurrence_key():
    settings = configured(start='23:00', end='02:00')
    before = datetime(2026, 10, 20, 23, 30)
    after = datetime(2026, 10, 21, 1, 30)
    assert ai_core.in_window(settings, before)
    assert ai_core.in_window(settings, after)
    assert ai_core.window_key(settings, before) == ai_core.window_key(settings, after)
    assert not ai_core.in_window(settings, datetime(2026, 10, 21, 2, 0))


def test_private_store_keeps_key_and_target_samples_private_and_separate(tmp_path):
    store = ai_core.PrivateAIStore(tmp_path)
    assert not store.key_configured()
    assert store.get_key() == ''
    store.set_key('secret-test-key')
    assert store.key_configured()
    assert ai_core.PrivateAIStore(tmp_path).get_key() == 'secret-test-key'
    store.set_samples('account', 'friend', 'target-one', ['你好', '晚点聊'])
    assert store.samples('account', 'friend', 'target-one') == ['你好', '晚点聊']
    assert store.samples('account', 'friend', 'target-two') == []
    assert store.samples('other', 'friend', 'target-one') == []
    files = [path for path in tmp_path.rglob('*') if path.is_file()]
    assert len(files) == 2
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in files)
    assert not any('secret-test-key' in path.name for path in files)
    store.set_key('')
    assert not store.key_configured()
    assert store.samples('account', 'friend', 'target-one') == ['你好', '晚点聊']


@pytest.mark.parametrize('samples', [['x'] * 51, ['x' * 501], [''], [123], 'hello'])
def test_private_store_rejects_invalid_samples(tmp_path, samples):
    with pytest.raises(ai_core.AIError):
        ai_core.PrivateAIStore(tmp_path).set_samples('account', 'friend', 'target', samples)


def row(server_id, created_at=100, **values):
    return {'server_id': server_id, 'client_id': '', 'from_me': False, 'sender': 'friend',
            'conversation_id': 'thread', 'created_at': created_at, 'message_type': 'text',
            'text': 'synthetic incoming', 'server_status': 0, **values}


def test_ledger_baselines_history_and_tracks_new_incoming_across_sessions(tmp_path,monkeypatch):
    monkeypatch.setattr(ai_core.time,'time',lambda:0)
    ledger = ai_core.ReplyLedger(tmp_path)
    old = row('old', 100)
    assert ledger.baseline('activation:day', 'account', 'target', [old])
    assert not ledger.baseline('activation:day', 'account', 'target', [old, row('new', 101)])
    assert ledger.unseen('activation:day', 'account', 'target', [old]) == []
    rows = [old, row('paginated-old', 90), row('same-second-old', 100), row('out', 101, from_me=True),
            row('', 102), row('no-time', None), row('new', 101), row('new', 101)]
    unseen = ledger.unseen('activation:day', 'account', 'target', rows)
    assert [item['incoming_id'] for item in unseen] == ['new']
    ledger.mark_seen('activation:day', 'account', 'target', unseen)
    assert ledger.unseen('activation:day', 'account', 'target', rows) == []
    ledger = ai_core.ReplyLedger(tmp_path)
    assert ledger.unseen('activation:day', 'account', 'target', rows) == []
    assert ledger.unseen('unknown-session', 'account', 'target', [row('fresh', 999)]) == []


def test_ledger_claim_is_durable_unique_and_never_stores_reply_plaintext(tmp_path):
    ledger = ai_core.ReplyLedger(tmp_path)
    attempt_id = ledger.reserve('account', 'target', 'incoming', 'friend', 'PRIVATE DRAFT TEXT')
    assert attempt_id
    assert ledger.blocked('account', 'target')
    assert ledger.reserve('account', 'target', 'incoming', 'friend', 'again') is None
    assert ledger.reserve('account', 'target', 'second', 'friend', 'again') is None
    ledger.transition(attempt_id, 'dispatching')
    ledger.transition(attempt_id, 'confirmed')
    assert not ledger.blocked('account', 'target')
    assert ai_core.ReplyLedger(tmp_path).reserve('account', 'target', 'incoming', 'friend', 'again') is None
    assert 'PRIVATE DRAFT TEXT' not in json.dumps(ledger.history())
    assert b'PRIVATE DRAFT TEXT' not in ledger.path.read_bytes()
    assert ledger.path.stat().st_mode & 0o777 == 0o600


def test_ledger_explicit_recovery_and_resolution_prevent_implicit_retries(tmp_path):
    ledger = ai_core.ReplyLedger(tmp_path)
    reserved = ledger.reserve('account', 'target', 'one', 'friend', 'reply')
    other = ledger.reserve('account', 'other', 'two', 'friend', 'reply')
    ledger.transition(other, 'dispatching')
    reopened = ai_core.ReplyLedger(tmp_path)
    assert {item['status'] for item in reopened.history()} == {'reserved', 'dispatching'}
    reopened.recover('account')
    by_id = {item['id']: item for item in reopened.history()}
    assert by_id[reserved]['status'] == 'failed'
    assert by_id[other]['status'] == 'uncertain'
    assert reopened.blocked('account', 'other')
    reopened.resolve(other)
    assert not reopened.blocked('account', 'other')
    assert reopened.reserve('account', 'other', 'two', 'friend', 'reply') is None
    with pytest.raises(ai_core.AIError):
        reopened.transition(other, 'dispatching')


class Response:
    def __init__(self, payload):
        self.payload = payload
        self.read_size = None
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False
    def read(self, size):
        self.read_size = size
        return self.payload[:size]


class Opener:
    def __init__(self, payload):
        self.response = Response(payload)
    def open(self, request, timeout):
        self.request, self.timeout = request, timeout
        return self.response


def response_payload(content='可以，晚上聊！', **choice):
    return json.dumps({'choices': [{'message': {'content': content}, 'finish_reason': 'stop', **choice}]}).encode()


def test_client_fixed_endpoint_bounded_request_and_plain_reply():
    opener = Opener(response_payload())
    result = ai_core.generate_reply('test-secret', ['样本'] * 50,
                                    [{'role': 'user', 'content': 'x' * 2000}] * 30, _opener=opener)
    assert result == '可以，晚上聊！'
    assert opener.request.full_url == 'https://api.deepseek.com/chat/completions'
    assert opener.request.get_header('Authorization') == 'Bearer test-secret'
    assert opener.timeout <= 25
    payload = json.loads(opener.request.data)
    assert payload['model'] == 'deepseek-flash'
    assert payload['thinking'] == {'type': 'disabled'}
    assert payload['max_tokens'] <= 256
    assert len(payload['messages']) <= 21
    assert all(len(item['content']) <= 1000 for item in payload['messages'][1:])
    assert opener.response.read_size <= 131073


def test_context_skill_reaches_model_with_clock_pending_and_provenance():
    from douyinfire.ai_style import reply_rules
    opener=Opener(response_payload('好呀'))
    payload={'current_time':'2026-10-20T20:00:00+08:00','timezone':'Asia/Shanghai',
             'messages':[{'id':'1','role':'user','content':'几点？','created_at':'2026-10-20T19:59:00+08:00'}],
             'pending_ids':['1'], 'style_examples':[{'content':'好呀','source':'human'}]}
    assert ai_core.generate_reply('test-secret',['好呀'],[{'role':'user','content':'几点？'}],
                                  chat_context=payload,_opener=opener)=='好呀'
    body=json.loads(opener.request.data)
    assert body['messages'][0]['content']==reply_rules()
    assert json.loads(body['messages'][1]['content'])==payload
    assert len(body['messages'])==2


@pytest.mark.parametrize('payload', [
    b'not json PRIVATE RAW BODY', b'{}', response_payload(''), response_payload('x' * 501),
    response_payload({'text': 'not string'}), response_payload('hello', finish_reason='length'),
    response_payload('```text\nhello\n```'), response_payload('<script>alert(1)</script>'),
    b'x' * 131074,
])
def test_client_invalid_results_are_sanitized(payload):
    with pytest.raises(ai_core.AIError) as caught:
        ai_core.generate_reply('test-secret', [], [{'role': 'user', 'content': 'hi'}], _opener=Opener(payload))
    assert 'PRIVATE' not in str(caught.value)
    assert 'RAW BODY' not in str(caught.value)


def test_client_sanitizes_transport_errors():
    class BrokenOpener:
        def open(self, request, timeout):
            raise RuntimeError('PRIVATE API KEY and raw network body')
    with pytest.raises(ai_core.AIError) as caught:
        ai_core.generate_reply('test-secret', [], [{'role': 'user', 'content': 'hi'}], _opener=BrokenOpener())
    assert 'PRIVATE' not in str(caught.value)


def test_production_transport_disables_proxies_and_redirects(monkeypatch):
    handlers = []
    def build(*items):
        handlers.extend(items)
        return Opener(response_payload())
    monkeypatch.setattr(ai_core.urllib.request, 'build_opener', build)
    assert ai_core.generate_reply('test-secret', [], [{'role': 'user', 'content': 'hi'}])
    assert any(isinstance(handler, ai_core.urllib.request.ProxyHandler) and handler.proxies == {} for handler in handlers)
    redirect = next(handler for handler in handlers if isinstance(handler, ai_core.urllib.request.HTTPRedirectHandler))
    with pytest.raises(Exception):
        redirect.http_error_302(None, None, 302, 'redirect', {'Location': 'https://example.com'})


def test_empty_baseline_does_not_pick_up_old_history_later(tmp_path, monkeypatch):
    monkeypatch.setattr(ai_core.time, 'time', lambda: 2_000)
    ledger = ai_core.ReplyLedger(tmp_path)
    assert ledger.baseline('session', 'account', 'target', [])
    assert ledger.unseen('session', 'account', 'target', [row('old', 1_999_000)]) == []
    assert ledger.unseen('session', 'account', 'target', [row('new', 2_001_000)])[0]['incoming_id'] == 'new'


def test_ledger_parallel_claims_have_exactly_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    ledger = ai_core.ReplyLedger(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as workers:
        attempts = list(workers.map(lambda _: ledger.reserve('account', 'target', 'one', 'friend'), range(16)))
    assert len([attempt for attempt in attempts if attempt]) == 1
    assert len(ledger.history()) == 1
    assert ledger.reserve('different-account', 'target', 'one', 'friend')
    assert ledger.reserve('account', 'different-target', 'one', 'friend')


def test_private_store_rejects_key_controls_without_overwriting_existing_key(tmp_path):
    store = ai_core.PrivateAIStore(tmp_path)
    store.set_key('original-secret')
    with pytest.raises(ai_core.AIError):
        store.set_key('other-secret\nInjected: header')
    assert store.get_key() == 'original-secret'


def test_private_store_does_not_follow_key_symlink(tmp_path):
    store = ai_core.PrivateAIStore(tmp_path)
    external = tmp_path / 'external-key'
    external.write_text('external-secret')
    store.key_path.symlink_to(external)
    with pytest.raises(ai_core.AIError):
        store.get_key()
    store.set_key('local-secret')
    assert external.read_text() == 'external-secret'
    assert store.get_key() == 'local-secret'


@pytest.mark.parametrize('content', ['**加粗**', '# 标题', '[点击](https://example.com)', '`code`', 'hey\x7f'])
def test_client_rejects_formatted_or_control_text(content):
    with pytest.raises(ai_core.AIError):
        ai_core.generate_reply('test-secret', [], [{'role': 'user', 'content': 'hi'}], _opener=Opener(response_payload(content)))


def test_ledger_never_logs_prose_details(tmp_path):
    ledger = ai_core.ReplyLedger(tmp_path)
    attempt = ledger.reserve('account', 'target', 'one', 'friend', 'PRIVATE DRAFT')
    result = ledger.transition(attempt, 'failed', 'PRIVATE DRAFT AND API KEY')
    assert result['detail'] == 'operation_failed'
    assert b'PRIVATE' not in ledger.path.read_bytes()


def test_baseline_wall_clock_excludes_delayed_pre_activation_history(tmp_path,monkeypatch):
    monkeypatch.setattr(ai_core.time,'time',lambda:200)
    ledger=ai_core.ReplyLedger(tmp_path)
    ledger.baseline('session','account','target',[row('100',100000)])
    later=[row('100',100000),row('150',150000),row('201',201000)]
    assert [r['incoming_id'] for r in ledger.unseen('session','account','target',later)]==['201']


def test_only_known_pre_dispatch_failure_is_reclaimable(tmp_path):
    ledger=ai_core.ReplyLedger(tmp_path)
    attempt=ledger.reserve('account','target','incoming','friend')
    ledger.transition(attempt,'failed','reply_failed')
    assert ledger.reserve('account','target','incoming','friend')==attempt
    ledger.transition(attempt,'dispatching')
    ledger.transition(attempt,'uncertain')
    assert ledger.reserve('account','target','incoming','friend') is None
    ledger.resolve(attempt)
    assert ledger.reserve('account','target','incoming','friend') is None


def test_all_covered_inputs_remain_handled_if_a_later_attempt_fails(tmp_path):
    ledger=ai_core.ReplyLedger(tmp_path)
    first=ledger.reserve('a','t','200','c',covered_ids=['150','200'])
    ledger.transition(first,'dispatching');ledger.transition(first,'confirmed')
    later=ledger.reserve('a','t','300','c',covered_ids=['150','300'])
    ledger.transition(later,'failed')
    assert [r['server_id'] for r in ledger.outstanding('a','t',[{'server_id':s} for s in ['150','200','300']])]==['300']
    assert ledger.reserve('a','t','150','c') is None


def media_fixture():
    import base64
    return [{'message_id':'42','kind':'video','text':'仅封面；音频不可用',
             'images':[{'label':'封面','data_url':'data:image/png;base64,'+base64.b64encode(b'\x89PNG\r\n\x1a\nrest').decode()}]}]


def media_context():
    return {'messages':[{'id':'41','role':'user','source':'contact','content':'看看这个'},
                        {'id':'42','role':'user','source':'contact','content':'[视频]','media':{'kind':'video'}}],
            'pending_ids':['41','42']}


def test_client_mixed_multimodal_wire_keeps_canonical_rules_and_provenance():
    from douyinfire.ai_style import reply_rules
    opener=Opener(response_payload('风景挺好'))
    context=media_context()
    ai_core.generate_reply('secret',[],[{'role':'user','content':'看看这个'}],
                           chat_context=context,media=media_fixture(),_opener=opener)
    body=json.loads(opener.request.data)
    assert body['messages'][0]['content'].startswith(reply_rules())
    assert 256 <= body['max_tokens'] <= 512
    content=body['messages'][1]['content']
    assert json.loads(content[0]['text'])==context
    assert '42' in content[1]['text'] and '仅封面' in content[1]['text']
    assert '42' in content[2]['text'] and '封面' in content[2]['text']
    assert content[3]=={'type':'image_url','image_url':{'url':media_fixture()[0]['images'][0]['data_url']}}
    assert '音频' in body['messages'][0]['content']


def test_client_allows_media_only_actual_pending_user_context():
    opener=Opener(response_payload())
    assert ai_core.generate_reply('secret',[],[],chat_context=media_context(),media=media_fixture(),_opener=opener)


@pytest.mark.parametrize('context', [
    {'messages':[], 'pending_ids':['42']},
    {'messages':[{'id':'42','role':'assistant','content':'[视频]'}], 'pending_ids':['42']},
    {'messages':[{'id':'42','role':'user','source':'other_contact','content':'[视频]'}], 'pending_ids':['42']},
    {'messages':[{'id':'42','role':'user','content':'[视频]'}], 'pending_ids':['missing']},
])
def test_client_media_only_rejects_missing_actual_pending(context):
    with pytest.raises(ai_core.AIError):
        ai_core.generate_reply('secret',[],[],chat_context=context,media=media_fixture(),_opener=Opener(response_payload()))


def test_client_rejects_external_or_oversized_images_before_transport():
    import base64
    for url in ['https://private.invalid', 'data:image/png;base64,'+base64.b64encode(b'\x89PNG\r\n\x1a\n'+b'x'*(2*1024*1024)).decode()]:
        media=media_fixture(); media[0]['images'][0]['data_url']=url
        with pytest.raises(ai_core.AIError) as caught:
            ai_core.generate_reply('secret',[],[{'role':'user','content':'hello'}],chat_context=media_context(),media=media,_opener=Opener(response_payload()))
        assert 'private.invalid' not in str(caught.value)
        assert 'secret' not in str(caught.value)
