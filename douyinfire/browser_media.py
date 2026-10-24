"""Transient media-only screenshots from an authenticated, verified chat.

No unrestricted fetching, raw media download, audio recognition or disk cache.
The caller retains the account lock and rechecks the pending snapshot afterward.
"""
from __future__ import annotations

import base64
import math
import re
from urllib.parse import urlsplit

from .browser import BrowserError, BrowserInterrupted, LoginRequired

_BOXES='[data-e2e="im-dialog"] .messageMessageBoxmessageBox'
_SERVER_ID="""e=>{const k=Object.keys(e).find(k=>k.startsWith('__reactProps'));const b=e[k]?.children?.[1]?.props?.children?.[0]?.props;return (b?.message??b?.children?.[1]?.props?.message)?.serverId;}"""
_VIDEO_ID="""(v,id)=>{const root=v.closest('[data-e2e="player-container"]');if(!root||!root.classList.contains('video_'+id))return false;for(let n=v.parentElement;n&&n!==root;n=n.parentElement){const k=Object.keys(n).find(k=>k.startsWith('__reactProps'));const p=n[k];const children=Array.isArray(p?.children)?p.children:[p?.children];if(children.some(c=>c?.props?.awemeInfo?.awemeId===id))return true;}return false;}"""


def _clean(value,limit=1200):
    return re.sub(r'[\x00-\x1f\x7f-\x9f]',' ',str(value or ''))[:limit]


def _video_url(video_id):
    if not isinstance(video_id,str) or not re.fullmatch(r'[0-9]{10,25}',video_id):
        raise BrowserError('视频标识尚未核实。')
    return 'https://www.douyin.com/video/'+video_id


def _frame_times(duration,count=3):
    if isinstance(duration,bool) or not isinstance(duration,(float,int)) or not math.isfinite(duration) or duration<=0:
        return []
    end=min(float(duration),90.0)
    return [min(max(.05,end*f),max(.05,float(duration)-.1)) for f in ([.1,.7] if count==2 else [.05,.5,.9])]


def _matches_video_url(url,video_id):
    value=urlsplit(url)
    return (value.scheme=='https' and value.hostname=='www.douyin.com' and not value.username
            and not value.password and value.port in (None,443) and value.path.rstrip('/')=='/video/'+video_id)


def _video_navigation(page,video_id):
    def route_request(route):
        request=route.request
        if (request.is_navigation_request() and request.frame==page.main_frame
                and not _matches_video_url(request.url,video_id)):
            route.abort()
        else:
            route.continue_()
    return route_request


def _video_guard(browser,page,identity,video_id):
    browser._check_interrupted()
    browser.read_direct_history(identity)
    if not _matches_video_url(page.url,video_id):
        raise BrowserError('视频页面已变化。')
    videos=page.locator('video:visible')
    if videos.count()!=1 or not videos.first.evaluate(_VIDEO_ID,video_id):
        raise BrowserError('视频画面身份尚未核实。')
    return videos.first


def _shot(locator,label):
    if not locator.is_visible():
        raise BrowserError('当前媒体画面不可见。')
    raw=locator.screenshot(type='jpeg',quality=72,timeout=5000,animations='disabled')
    if not raw.startswith(b'\xff\xd8\xff') or len(raw)>2*1024*1024:
        raise BrowserError('媒体画面超过大小限制。')
    return {'data_url':'data:image/jpeg;base64,'+base64.b64encode(raw).decode('ascii'),'label':_clean(label,256)}


def _box(browser,message_id):
    boxes=browser._require_page().locator(_BOXES)
    if boxes.count()>300:
        raise BrowserError('当前消息列表超过读取范围。')
    found=[]
    for index in range(boxes.count()):
        browser._check_interrupted()
        item=boxes.nth(index)
        if item.evaluate(_SERVER_ID)==message_id:
            found.append(item)
    if len(found)!=1:
        raise BrowserError('媒体消息未唯一加载。')
    return found[0]


def _images(browser,identity,row):
    box=_box(browser,str(row['server_id']))
    media=row.get('media') or {}
    if media.get('kind')=='video':
        selector='img.MessageItemShareAwemeawemeContainer, img.MessageItemCommentShareawemeCover'
        label='视频封面（只提供封面，不能据此推断完整视频）'
    else:
        selector='[data-e2e="msg-item-content"] img, img.MessageItemEmojiimage'
        label='聊天中的图片或表情包画面'
    items=box.locator(selector)
    result=[]
    for index in range(min(items.count(),3)):
        browser.read_direct_history(identity)
        if box.evaluate(_SERVER_ID)!=str(row['server_id']):
            raise BrowserInterrupted('媒体消息已变化。')
        item=items.nth(index)
        if item.is_visible():
            result.append(_shot(item,label))
    return result


def _video_frames(browser,identity,row):
    media=row.get('media') or {}
    url=_video_url(media.get('video_id'))
    page=browser.context.new_page()
    try:
        browser._check_interrupted()
        page.route('**/*',_video_navigation(page,media['video_id']))
        # Keep playback inaudible; this does not record or transcribe audio.
        page.add_init_script("document.addEventListener('play',e=>{if(e.target instanceof HTMLMediaElement)e.target.muted=true},true)")
        page.goto(url,wait_until='domcontentloaded',timeout=20000)
        if not _matches_video_url(page.url,media['video_id']):
            raise BrowserError('视频页面未通过核对。')
        page.wait_for_function("""()=>Array.from(document.querySelectorAll('video')).filter(v=>v.getBoundingClientRect().width>0&&v.getBoundingClientRect().height>0&&v.readyState>=2&&Number.isFinite(v.duration)&&v.duration>0).length===1""",timeout=12000)
        # The observed detail page has one visible video, plus an invisible
        # precreated player. Do not choose a feed video by index.
        video=_video_guard(browser,page,identity,media['video_id'])
        duration=video.evaluate('v=>v.duration')
        output=[]
        comment=bool(media.get('comment_share'))
        if comment:
            candidates=_box(browser,str(row['server_id'])).locator('img.MessageItemCommentSharecommentCover')
            if candidates.count()==1 and candidates.is_visible():
                output.append(_shot(candidates,'分享的视频评论截图（不是视频帧）'))
        for stamp in _frame_times(duration,2 if comment else 3):
            video=_video_guard(browser,page,identity,media['video_id'])
            video.evaluate("""(v,t)=>new Promise(resolve=>{v.pause();v.muted=true;if(Math.abs(v.currentTime-t)<.05){resolve();return;}const done=()=>resolve();v.addEventListener('seeked',done,{once:true});v.currentTime=t;setTimeout(done,2000);})""",stamp)
            page.wait_for_timeout(150)
            if abs(video.evaluate('v=>v.currentTime')-stamp)>1 or video.evaluate('v=>v.readyState')<2:
                raise BrowserError('视频关键帧尚未加载。')
            _video_guard(browser,page,identity,media['video_id'])
            output.append(_shot(video,f'视频约 {stamp:.1f} 秒的画面；没有音频'))
            _video_guard(browser,page,identity,media['video_id'])
        return output,f'读取视频关键帧；总时长约 {duration:.1f} 秒，仅抽样前 {min(duration,90):.1f} 秒，未读取音轨。'
    finally:
        page.close()


def prepare_incoming_media(browser,identity,rows):
    from .ai_media import supported_incoming,validated_observations
    if not isinstance(rows,list) or len(rows)>4:
        raise BrowserError('单次最多读取四条媒体消息。')
    observations=[]
    for row in rows:
        browser.read_direct_history(identity)
        if row.get('from_me') or not supported_incoming(row):
            raise BrowserError('媒体不属于当前待回复来信。')
        media=row.get('media') or {}
        kind=media.get('kind') or ('sticker' if row.get('expression') else '')
        if kind not in {'video','image','sticker'}:
            raise BrowserError('媒体类型尚未核实。')
        title=_clean(media.get('title') or (row.get('expression') or {}).get('name'))
        text=f'{title}；媒体无法读取，不能推断画面内容。'
        images=[]
        try:
            if kind=='video':
                try:
                    images,coverage=_video_frames(browser,identity,row)
                    text=title+'；'+coverage
                except (BrowserInterrupted,LoginRequired):
                    raise
                except Exception:
                    images=_images(browser,identity,row)[:1]
                    text=title+('；仅能读取视频封面，视频本身未成功播放，未读取音轨。' if images
                                else '；视频和封面均无法读取，不能推断画面内容，未读取音轨。')
            else:
                images=_images(browser,identity,row)
                text=title+('；已读取聊天中显示的媒体画面。' if images else '；没有可读取的画面，不能推断内容。')
        except (BrowserInterrupted,LoginRequired):
            raise
        except Exception:
            images=[]
        browser.read_direct_history(identity)
        observations.append({'message_id':str(row['server_id']),'kind':kind,'text':_clean(text,4000),'images':images})
    try:
        return validated_observations(observations)
    except ValueError:
        raise BrowserError('媒体总大小超过限制。') from None
