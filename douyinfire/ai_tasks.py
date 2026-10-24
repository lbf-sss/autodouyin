"""Bounded AI reply polling. All browser work shares the existing account lock."""
from __future__ import annotations

from datetime import datetime

from .ai_core import AIError, PrivateAIStore, ReplyLedger, generate_reply, in_window, window_key
from .browser import BrowserError, BrowserInterrupted, SendUncertain
from .ai_media import supported_incoming, message_text
from .config import load_config, ensure_runtime_dirs
from .tasks import _account_lock, _browser
from .ai_style import StyleArchive, first_ai_time, human_samples, reply_context, learning_status


def _now():
    return datetime.now().astimezone()


def _contact(user, contact_id):
    contact = next((c for c in user.contacts if c.id == contact_id), None)
    if (contact is None or not user.enabled or not user.identity or not contact.enabled
            or contact.kind != 'direct' or not contact.identity or not contact.profile_url):
        raise ValueError('请选择已绑定身份并启用的私聊联系人。')
    return contact


def import_samples(config, user, contact_id, *, progress=None, should_stop=None):
    contact = _contact(user, contact_id)
    ensure_runtime_dirs(config)
    with _account_lock(config, user):
        with _browser(config, user, 'ai-samples', progress=progress, should_stop=should_stop) as browser:
            rows = browser.open_direct_history(contact.profile_url, contact.identity)
        if should_stop and should_stop():
            raise BrowserInterrupted('已取消读取语气样本。')
        # Only our own mounted plain text; never store the friend's messages.
        samples = human_samples(rows, first_ai_time(config.data_dir, user.id))[-50:]
        if not samples:
            raise ValueError('当前加载的聊天没有本人文字。可在语气样本框手动补充。')
        PrivateAIStore(config.data_dir).set_samples(user.id, contact.id, contact.identity, samples)
    return {'contact_id': contact.id, 'sample_count': len(samples)}


def learn_account_style(config, user, *, progress=None, should_stop=None):
    if not user.enabled or not user.identity:
        raise ValueError('请先登录并核实账号。')
    contacts=[c for c in user.contacts if c.enabled and c.kind=='direct' and c.identity and c.profile_url]
    if not contacts or len(contacts)>200:
        raise ValueError('请先保存并绑定私聊联系人（单次最多 200 个）。')
    ensure_runtime_dirs(config)
    archive=StyleArchive(config.data_dir)
    previous=archive.profile(user.id,user.identity)
    learned=[]
    failures=0
    with _account_lock(config,user):
        cutoff=first_ai_time(config.data_dir,user.id)
        with _browser(config,user,'ai-learn',progress=progress,should_stop=should_stop) as browser:
            for contact in contacts:
                if should_stop and should_stop():
                    raise BrowserInterrupted('已取消历史语气学习。')
                if progress:
                    progress('ai_learn','running',contact.name)
                try:
                    result=browser.collect_direct_history(contact.profile_url,contact.identity)
                    samples=human_samples(result['rows'],cutoff)
                    old=next((c for c in previous.get('contacts',[]) if
                              c['contact_id']==contact.id and c['identity']==contact.identity),{})
                    samples=list(dict.fromkeys(old.get('samples',[])+samples))[-600:]
                    learned.append({'contact_id':contact.id,'identity':contact.identity,
                                    'samples':samples,'loaded_count':max(old.get('loaded_count',0),len(result['rows'])),
                                    'stop_reason':result['stop_reason']})
                except BrowserInterrupted:
                    raise
                except BrowserError:
                    failures+=1
                    # Keep earlier evidence for the same binding on partial failure.
                    old=next((c for c in previous.get('contacts',[]) if
                              c['contact_id']==contact.id and c['identity']==contact.identity),None)
                    if old:
                        learned.append(old)
        if should_stop and should_stop():
            raise BrowserInterrupted('已取消历史语气学习。')
        if not learned:
            raise ValueError('未能读取历史消息；已保留原有语气记录。')
        archive.save(user.id,user.identity,learned,limited=True,failed_count=failures)
    return learning_status(config.data_dir,user.id,user.identity)


def _fingerprint(rows):
    # Signed resource URLs expire independently of a message. Track stable content
    # identity so replacement media still cancels an already prepared reply.
    fields=('kind','name','title','id','video_id','media_id','aweme_id','resource_id')
    return [(r['server_id'], r['from_me'], r['message_type'], r.get('text'),
             tuple((r.get('media') or {}).get(k) for k in fields),
             tuple((r.get('expression') or {}).get(k) for k in ('id','name')))
            for r in rows]


def _same_current_history(original, current):
    if not original:
        return not current
    # Extra records strictly older than the original viewport are lazy history
    # hydration, not a new incoming message or a manual reply.
    first=min((r['created_at'],int(r['server_id'])) for r in original)
    relevant=[r for r in current if (r['created_at'],int(r['server_id']))>=first]
    return _fingerprint(relevant)==_fingerprint(original)


def _pending_text(rows):
    """Outstanding supported incoming messages after the last actual reply."""
    last_reply = -1
    for index, row in enumerate(rows):
        renewal = row['message_type'] == 5 and (row.get('expression') or {}).get('name') == '续火花'
        if row['from_me'] and not renewal:
            last_reply = index
    return [row for row in rows[last_reply + 1:]
            if not row['from_me'] and supported_incoming(row)]


def _allowed(config_path, started, user, contact, should_stop):
    if should_stop and should_stop():
        raise BrowserInterrupted('自动回复已暂停。')
    current = load_config(config_path)
    a, old = current.ai_reply, started.ai_reply
    if (not a.enabled or not in_window(a, _now()) or a != old
            or a.user_id != user.id or contact.id not in a.contact_ids):
        raise BrowserInterrupted('自动回复设置或时间段已变化。')
    latest_user = current.user(user.id)
    latest = _contact(latest_user, contact.id)
    if latest.identity != contact.identity or latest_user.identity != user.identity or latest.profile_url != contact.profile_url:
        raise BrowserInterrupted('账号或联系人绑定已变化。')
    return current


def poll_replies(config_path, user_id, *, progress=None, should_stop=None):
    config = load_config(config_path)
    settings = config.ai_reply
    result = {'ai_reply': True, 'checked': 0, 'confirmed': 0, 'skipped': 0, 'uncertain': 0, 'contacts': []}
    if not settings.enabled or settings.user_id != user_id or not in_window(settings, _now()):
        return result
    user = config.user(user_id)
    session = settings.generation + ':' + window_key(settings, _now())
    store, ledger = PrivateAIStore(config.data_dir), ReplyLedger(config.data_dir)
    if not store.key_configured():
        raise AIError('请先在程序内填写 DeepSeek API Key。')
    with _account_lock(config, user):
        ledger.recover(user.id)
        for contact_id in settings.contact_ids:
            # Douyin can retain an empty message store after a profile switch.
            # Reopen the same authenticated profile for each target under the
            # account lock, so every chat starts with a fresh page/session.
            with _browser(config, user, 'ai-poll', progress=progress, should_stop=should_stop) as browser:
                contact = _contact(user, contact_id)
                _allowed(config_path, config, user, contact, should_stop)
                if ledger.blocked(user.id, contact.identity):
                    result['uncertain'] += 1
                    continue
                archive=StyleArchive(config.data_dir)
                samples = list(dict.fromkeys(archive.samples(user.id,user.identity,contact.id,contact.identity)
                                             +store.samples(user.id, contact.id, contact.identity)))
                if progress:
                    progress('ai_poll', 'running', contact.name)
                rows = browser.open_direct_history(contact.profile_url, contact.identity)
                result['checked'] += 1
                incoming = ledger.outstanding(user.id, contact.identity, _pending_text(rows))
                result['contacts'].append({'contact_id':contact.id,'loaded':len(rows),'pending':len(incoming)})
                if progress:
                    progress('ai_poll','running',f'{contact.name} · 已读 {len(rows)} 条，待回复 {len(incoming)} 条')
                if not incoming:
                    continue
                latest = incoming[-1]
                latest_index = next(i for i, row in enumerate(rows) if row['server_id'] == latest['server_id'])
                attempt = ledger.reserve(user.id, contact.identity, latest['server_id'], contact.id, '',
                                         covered_ids=[r['server_id'] for r in incoming])
                if attempt is None:
                    continue
                try:
                    _allowed(config_path, config, user, contact, should_stop)
                    def guard():
                        _allowed(config_path, config, user, contact, should_stop)
                        if settings.generation + ':' + window_key(settings, _now()) != session:
                            raise BrowserInterrupted('时间段已变化。')
                        if not _same_current_history(rows,browser.read_direct_history(contact.identity)):
                            raise BrowserInterrupted('消息已变化，已放弃旧回复。')
                    media_pending=[r for r in incoming if r['message_type'] != 7
                                   or (r.get('media') or {}).get('kind') in ('image','sticker','video')]
                    media_options={}
                    if media_pending:
                        if len(media_pending)>4:
                            raise AIError('待回复媒体超过限制，请手动处理部分消息后重试。')
                        observations=browser.prepare_incoming_media(contact.identity,media_pending)
                        guard()
                        expected={str(r['server_id']) for r in media_pending}
                        if (not isinstance(observations,list)
                                or {str(o.get('message_id')) for o in observations if isinstance(o,dict)} != expected
                                or len(observations)!=len(expected)):
                            raise AIError('媒体读取不完整，已放弃本次回复。')
                        media_options['media']=observations
                    context = [{'role':'assistant' if r['from_me'] else 'user','content':message_text(r)[:1000]}
                               for r in rows[:latest_index + 1] if supported_incoming(r)][-20:]
                    structured=reply_context(rows[:latest_index+1],incoming,contact_name=contact.name,
                                             samples=samples,account_style=archive.features(user.id,user.identity),
                                             now=_now(),timezone=settings.timezone)
                    reply = generate_reply(store.get_key(), structured['style_examples'] and
                                           [s['content'] for s in structured['style_examples']], context,
                                           chat_context=structured,**media_options)
                    guard()
                    browser.send_current_reply(contact.identity, reply,
                        before_send=lambda: ledger.transition(attempt, 'dispatching'), guard=guard)
                    ledger.transition(attempt, 'confirmed')
                    result['confirmed'] += 1
                except SendUncertain:
                    ledger.transition(attempt, 'uncertain', 'receipt_unknown')
                    result['uncertain'] += 1
                except BrowserInterrupted:
                    ledger.transition(attempt, 'failed', 'reply_cancelled')
                    result['skipped'] += 1
                except Exception:
                    ledger.transition(attempt, 'failed', 'reply_failed')
                    result['skipped'] += 1
    return result
