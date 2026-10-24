"""UI contract tests with fabricated accounts and intercepted HTTP only."""
from copy import deepcopy
from urllib.parse import urlsplit

import pytest

from douyinfire.gui_html import HTML


@pytest.fixture
def ai_page():
    playwright = pytest.importorskip('playwright.sync_api')
    config = {'users': [
        {'id': 'a', 'name': '测试 A', 'enabled': True, 'contacts': [
            {'id': 'friend', 'name': '示例好友', 'identity': 'fake-friend', 'enabled': True},
            {'id': 'second', 'name': '测试朋友', 'identity': 'fake-second', 'enabled': True},
            {'id': 'group', 'name': '群聊', 'identity': 'fake-group', 'kind': 'group'},
            {'id': 'unbound', 'name': '未绑定'},
            {'id': 'off', 'name': '停用', 'identity': 'fake-off', 'enabled': False}]},
        {'id': 'b', 'name': '测试 B', 'contacts': [
            {'id': 'other', 'name': '另一个账号好友', 'identity': 'fake-other'}]}],
        'schedule': {}, 'ai_reply': {'enabled': False, 'user_id': 'a', 'contact_ids': ['friend'],
                                     'start': '19:00', 'end': '21:00', 'timezone': 'Asia/Shanghai'}}
    state = {'config': config, 'revision': 'test-revision', 'manager': {'running': False, 'auth': {}},
             'service': {'status': 'running'}}
    metadata = {'key_configured': True, 'sample_counts': {'friend': 1},
                'history': [{'id': 'uncertain-1', 'contact_id': 'friend', 'status': 'uncertain',
                             'detail': 'receipt_unknown', 'created_at': '2026-10-20'}]}
    samples = {('a', 'friend'): ['合成的本人发言'], ('a', 'second'): [], ('b', 'other'): []}
    posts, reads, errors = [], [], []

    def handle(route):
        req = route.request
        url = urlsplit(req.url)
        if url.path == '/':
            route.fulfill(status=200, content_type='text/html', body=HTML)
            return
        path = url.path
        result = {}
        if req.method == 'POST':
            body = req.post_data_json
            posts.append((path, body))
            if path == '/api/ai/settings':
                config['ai_reply'].update(body)
            elif path == '/api/ai/pause':
                config['ai_reply']['enabled'] = False
                state['manager']['running'] = False
            elif path == '/api/ai/key':
                metadata['key_configured'] = bool(body['key'])
                config['ai_reply']['enabled'] = False
            elif path == '/api/ai/samples':
                samples[(body['user_id'], body['contact_id'])] = body['samples']
                metadata['sample_counts'][body['contact_id']] = len(body['samples'])
                config['ai_reply']['enabled'] = False
            elif path == '/api/ai/resolve':
                metadata['history'][0]['status'] = 'resolved'
            elif path == '/api/jobs':
                config['ai_reply']['enabled'] = False
                state['manager'].update(running=True, kind=body['kind'], job_id='import-1', user_id=body['user_id'])
                result = {'job': deepcopy(state['manager'])}
        elif path == '/api/state':
            result = deepcopy(state)
        elif path == '/api/history':
            result = {'attempts': []}
        elif path == '/api/ai':
            reads.append(req.url)
            result = deepcopy(metadata)
        elif path == '/api/ai/samples':
            from urllib.parse import parse_qs
            query = parse_qs(url.query)
            reads.append(req.url)
            result = {'samples': samples[(query['user_id'][0], query['contact_id'][0])]}
        else:
            raise AssertionError(f'Unexpected intercepted request: {req.url}')
        import json
        route.fulfill(status=200, content_type='application/json', body=json.dumps(result))

    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 390, 'height': 844})
        page.route('**/*', handle)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('dialog', lambda dialog: dialog.accept())
        page.goto('http://ai-ui.test/')
        playwright.expect(page.locator('#aiKeyStatus')).to_contain_text('已配置')
        # Disable the timer; tests explicitly refresh synthetic data as needed.
        page.evaluate('for(let i=1;i<20;i++)clearInterval(i)')
        yield page, state, metadata, posts, reads
        assert errors == []
        browser.close()


def test_ai_form_defaults_whitelist_privacy_and_dirty_refresh(ai_page):
    page, state, metadata, posts, reads = ai_page
    assert page.locator('#aiEnabled').is_checked() is False
    assert page.locator('#aiStart').input_value() == '19:00'
    assert page.locator('#aiEnd').input_value() == '21:00'
    assert page.locator('[data-ai-contact]').count() == 2
    assert page.locator('[data-ai-contact="friend"]').is_checked()
    assert page.locator('[data-ai-count="friend"]').inner_text() == '1 条手动样本'
    assert page.locator('#aiKey').input_value() == ''
    assert page.locator('#aiKey').get_attribute('type') == 'password'
    assert page.locator('#aiSamples').input_value() == ''
    assert not any('/api/ai/samples?' in url for url in reads)
    page.locator('#aiStart').fill('18:30')
    page.locator('[data-ai-contact="second"]').check()
    metadata['sample_counts']['friend'] = 2
    page.evaluate('refresh()')
    assert page.locator('#aiStart').input_value() == '18:30'
    assert page.locator('[data-ai-contact="second"]').is_checked()
    assert page.locator('[data-ai-count="friend"]').inner_text() == '2 条手动样本'
    page.locator('#aiLoadSamples').click()
    page.wait_for_function("!submitting")
    assert page.locator('#aiSamples').input_value() == '合成的本人发言'
    page.locator('#aiSamples').fill('审核后编辑的合成文字')
    page.evaluate('refresh()')
    assert page.locator('#aiSamples').input_value() == '审核后编辑的合成文字'
    assert page.locator('#aiSave').is_disabled()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert posts == []


def test_ai_save_samples_then_explicit_activation_key_and_global_pause(ai_page):
    page, state, metadata, posts, reads = ai_page
    page.locator('#aiSamples').fill('合成一句\n合成两句')
    page.locator('#aiSaveSamples').click()
    page.wait_for_function("!submitting")
    assert posts[-1] == ('/api/ai/samples', {'user_id': 'a', 'contact_id': 'friend', 'samples': ['合成一句', '合成两句']})
    assert page.locator('#aiEnabled').is_checked() is False
    page.locator('#aiEnabled').check()
    page.locator('#aiSave').click()
    page.wait_for_function("!submitting")
    assert posts[-1] == ('/api/ai/settings', {'revision': 'test-revision', 'enabled': True,
                                           'user_id': 'a', 'contact_ids': ['friend'], 'start': '19:00', 'end': '21:00'})
    page.locator('#aiKey').fill('synthetic-test-key')
    page.locator('#aiSaveKey').click()
    page.wait_for_function("!submitting")
    assert page.locator('#aiKey').input_value() == ''
    assert page.locator('#aiEnabled').is_checked() is False
    assert 'synthetic-test-key' not in page.evaluate('JSON.stringify(localStorage)')
    state['manager']['running'] = True
    page.evaluate('refresh()')
    assert page.locator('#aiStart').is_disabled()
    assert page.locator('#aiSaveKey').is_disabled()
    assert page.locator('#aiPause').is_enabled()
    # Pause is also usable while another request is submitting.
    page.evaluate('submitting=true;renderControls()')
    page.locator('#aiPause').click()
    page.wait_for_function("document.getElementById('aiNotice').textContent==='已暂停自动回复。'")
    assert posts[-1] == ('/api/ai/pause', {})
    assert state['manager']['running'] is False
    page.evaluate('submitting=false;renderControls()')


def test_whitelist_can_add_contact_with_zero_samples(ai_page):
    page,state,metadata,posts,reads=ai_page
    assert not metadata['sample_counts'].get('second')
    page.locator('[data-ai-contact="second"]').check()
    page.locator('#aiEnabled').check()
    page.locator('#aiSave').click()
    page.wait_for_function('!submitting')
    assert posts[-1][0]=='/api/ai/settings'
    assert posts[-1][1]['contact_ids']==['friend','second']
    assert page.locator('#error').inner_text()==''


def test_account_switch_never_mutates_global_whitelist_and_resolve_is_ack_only(ai_page):
    page, state, metadata, posts, reads = ai_page
    page.locator('#aiStart').fill('18:30')
    page.locator('#accountSelect').select_option('b')
    page.wait_for_function("aiFormUserId==='b'")
    assert page.locator('[data-ai-contact="other"]').is_checked() is False
    assert page.locator('#aiEnabled').is_checked() is False
    assert state['config']['ai_reply']['contact_ids'] == ['friend']
    assert posts == []
    page.locator('#accountSelect').select_option('a')
    page.wait_for_function("aiFormUserId==='a'")
    assert page.locator('#aiStart').input_value() == '19:00'
    page.locator('#aiHistory').evaluate('(el)=>el.closest("details").open=true')
    page.locator('[data-ai-resolve]').click()
    page.wait_for_function("!submitting")
    assert posts == [('/api/ai/resolve', {'id': 'uncertain-1'})]
    assert page.locator('[data-ai-resolve]').count() == 0
    assert '原消息不重试' in page.locator('#aiHistory').inner_text()


def test_sample_limits_import_completion_and_drafts_survive_refresh(ai_page):
    page, state, metadata, posts, reads = ai_page
    page.locator('#aiSamples').fill('\n'.join('合成样本' for _ in range(51)))
    page.locator('#aiSaveSamples').click()
    page.wait_for_function("!submitting")
    assert '最多 50 条' in page.locator('#error').inner_text()
    assert posts == []
    page.locator('#aiSamples').fill('文' * 501)
    page.locator('#aiSaveSamples').click()
    page.wait_for_function("!submitting")
    assert '最多 500 字' in page.locator('#error').inner_text()
    assert posts == []
    page.locator('#aiSamples').fill('保留审核中的合成样本')
    page.locator('#aiImportSamples').click()
    page.wait_for_function("!submitting")
    assert posts[-1] == ('/api/jobs', {'kind': 'ai_samples', 'user_id': 'a', 'payload': {'contact_id': 'friend'}})
    assert page.locator('#aiPause').is_enabled()
    state['manager'].update(running=False, result={'contact_id': 'friend', 'sample_count': 3})
    page.evaluate('refresh()')
    assert '已导入 3 条样本' in page.locator('#aiNotice').inner_text()
    assert page.locator('#aiSamples').input_value() == '保留审核中的合成样本'
    assert page.locator('#aiEnabled').is_checked() is False
    assert not any('/api/ai/samples?' in url for url in reads)
