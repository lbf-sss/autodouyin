from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from douyinfire.browser import DouyinDOMAdapter,BrowserError,BrowserInterrupted
from douyinfire import browser_media


@pytest.fixture
def page():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b=p.chromium.launch(headless=True)
        page=b.new_page()
        page.route('http**://**',lambda r:r.abort())
        page.set_content('''<section data-e2e="im-dialog"><div class="messageMessageBoxmessageBox"><img id="avatar" width="30" height="30"><div data-e2e="msg-item-content" id="content"></div></div></section>''')
        yield page
        b.close()


def sdk(page,kind,parsed,content=''):
    page.locator('#content').evaluate('(e,s)=>e.innerHTML=s',content)
    page.locator('.messageMessageBoxmessageBox').evaluate('''(e,d)=>{e.__reactPropsFixture={children:[null,{props:{children:[{props:{message:{serverId:'123',clientId:'client',serverStatus:0,isFromMe:false,secSender:'friend',conversationId:'chat',conversationType:1,createdAt:new Date(1000),type:d.kind,parsedContent:d.parsed}}}]}}]}}''',dict(kind=kind,parsed=parsed))
    return page.locator('.messageMessageBoxmessageBox').evaluate_all(DouyinDOMAdapter._MESSAGES)[0]


def test_observed_video_and_comment_sdk_shapes_are_not_images(page):
    r=sdk(page,8,{'itemId':'1234567890123456789','content_title':'Synthetic title'},'<img>')
    assert r['media']['kind']=='video'
    assert r['media']['video_id']=='1234567890123456789'
    assert sdk(page,105,{'itemId':'1234567890123456789','aweme_title':'title','comment':'quoted'})['media']['comment_share']


def test_image_only_content_is_detected_but_avatar_is_not(page):
    assert sdk(page,2,{})['media'] is None
    assert sdk(page,2,{},'<img width="80" height="50">')['media']['kind']=='image'


def test_native_expression_is_detected(page):
    assert sdk(page,5,{'display_name':'比心','aweType':507})['media']['kind']=='sticker'


def test_image_capture_is_transient_and_scoped_to_message_content(page):
    src='data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="80" height="50"%3E%3Crect width="80" height="50" fill="red"/%3E%3C/svg%3E'
    row=sdk(page,2,{},f'<img src=\'{src}\' width="80" height="50">')
    browser=SimpleNamespace(_require_page=lambda:page,_check_interrupted=lambda:None,read_direct_history=Mock(return_value=[row]))
    result=browser_media.prepare_incoming_media(browser,'friend',[row])
    assert len(result)==1 and len(result[0]['images'])==1
    assert result[0]['images'][0]['data_url'].startswith('data:image/jpeg;base64,')
    assert result[0]['message_id']=='123'


def test_cancel_and_own_media_never_capture():
    b=SimpleNamespace(read_direct_history=Mock(side_effect=BrowserInterrupted('cancel')))
    with pytest.raises(BrowserInterrupted):browser_media.prepare_incoming_media(b,'friend',[{}])
    b.read_direct_history=Mock(return_value=[])
    with pytest.raises(BrowserError):browser_media.prepare_incoming_media(b,'friend',[{'from_me':True,'message_type':5,'expression':{'name':'续火花'}}])


def test_video_id_rejects_url_injection_and_frame_sampling_is_bounded():
    with pytest.raises(BrowserError):browser_media._video_url('https://other/evil')
    assert max(browser_media._frame_times(10000))<=90
    assert browser_media._frame_times(float('inf'))==[]


def test_video_guard_rejects_changed_url_and_wrong_player():
    b=SimpleNamespace(_check_interrupted=Mock(),read_direct_history=Mock())
    page=Mock(url='https://other.invalid/unrelated')
    with pytest.raises(BrowserError):browser_media._video_guard(b,page,'friend','1234567890123456789')
    page.url='https://www.douyin.com/video/1234567890123456789'
    page.locator.return_value.count.return_value=1
    page.locator.return_value.first.evaluate.return_value=False
    with pytest.raises(BrowserError):browser_media._video_guard(b,page,'friend','1234567890123456789')


def test_observed_player_identity_requires_matching_container_and_react_data(page):
    page.set_content('<div data-e2e="player-container" class="video_1234567890123456789"><div id="binding"><div><video></video></div></div></div>')
    page.locator('#binding').evaluate("e=>e.__reactPropsFixture={children:[{props:{awemeInfo:{awemeId:'1234567890123456789'}}}]}")
    assert page.locator('video').evaluate(browser_media._VIDEO_ID,'1234567890123456789')
    assert not page.locator('video').evaluate(browser_media._VIDEO_ID,'9999999999999999999')
    page.locator('#binding').evaluate("e=>e.__reactPropsFixture.children[0].props.awemeInfo.awemeId='9999999999999999999'")
    assert not page.locator('video').evaluate(browser_media._VIDEO_ID,'1234567890123456789')


def test_media_page_navigation_blocks_unexpected_top_level_destination():
    page=Mock()
    request=Mock(url='https://other.invalid/unrelated',frame=page.main_frame)
    request.is_navigation_request.return_value=True
    route=Mock(request=request)
    browser_media._video_navigation(page,'1234567890123456789')(route)
    route.abort.assert_called_once()
    route.continue_.assert_not_called()
    request.url='https://www.douyin.com/video/1234567890123456789'
    browser_media._video_navigation(page,'1234567890123456789')(route)
    route.continue_.assert_called_once()


def test_unavailable_video_does_not_claim_a_cover_was_read(monkeypatch):
    b=SimpleNamespace(read_direct_history=Mock(),_check_interrupted=Mock())
    row={'server_id':'123','from_me':False,'message_type':8,'media':{'kind':'video','video_id':'1234567890123456789'}}
    monkeypatch.setattr(browser_media,'_video_frames',Mock(side_effect=BrowserError('unavailable')))
    monkeypatch.setattr(browser_media,'_images',Mock(return_value=[]))
    result=browser_media.prepare_incoming_media(b,'friend',[row])
    assert result[0]['images']==[]
    assert '均无法读取' in result[0]['text']
    assert '仅能读取视频封面' not in result[0]['text']
