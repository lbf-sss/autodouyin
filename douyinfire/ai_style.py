"""Local human style evidence and bounded, time-aware reply context."""
from __future__ import annotations

import json
import re
import statistics
from datetime import datetime, timezone as utc_timezone
from importlib.resources import files
from zoneinfo import ZoneInfo

from .ai_media import supported_incoming, message_text
from .ai_core import AIError, PrivateAIStore, _PRIVATE_LOCK, _private_read, _private_write, _target_key


def reply_rules():
    try:
        return files('douyinfire').joinpath('skills/contextual-chat-style/references/reply-rules.md').read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        raise AIError('语境回复 skill 缺失，请重新安装程序。') from None


def human_samples(rows, cutoff=None):
    # Before the account's first AI attempt is a conservative provenance bound.
    # Later own messages may be human or AI; neither is silently self-training.
    return list(dict.fromkeys(r['text'].strip() for r in rows
        if r.get('from_me') and r.get('message_type') == 7
        and isinstance(r.get('text'), str) and 0 < len(r['text'].strip()) <= 500
        and (cutoff is None or 0 < r.get('created_at', 0) < cutoff)))[-600:]


def first_ai_time(data_dir, user_id):
    from .ai_core import ReplyLedger
    ledger = ReplyLedger(data_dir)
    with ledger._db() as db:
        value = db.execute('SELECT MIN(created_at) FROM attempts WHERE user_id=?', (user_id,)).fetchone()[0]
    return datetime.fromisoformat(value).timestamp()*1000 if value else None


class StyleArchive:
    def __init__(self, data_dir):
        self.path = PrivateAIStore(data_dir).root / 'style-history.json'

    def _read(self):
        try:
            value = _private_read(self.path, 16*1024*1024)
            data = json.loads(value) if value else {}
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, OSError, UnicodeError):
            raise AIError('无法读取本机历史语气记录。') from None

    def save(self, user_id, owner_identity, contacts, *, limited=True, failed_count=0):
        if len(contacts) > 200:
            raise AIError('本次学习会话数量超过限制。')
        profile = {'updated_at':datetime.now(utc_timezone.utc).isoformat(timespec='seconds'),
                   'limited':bool(limited), 'failed_count':failed_count, 'contacts':contacts}
        with _PRIVATE_LOCK:
            data=self._read()
            data[_target_key(user_id, 'account-style', owner_identity)]=profile
            serialized=json.dumps(data, ensure_ascii=False)
            if len(serialized)>16*1024*1024:
                raise AIError('本机历史语气记录超过大小限制。')
            _private_write(self.path, serialized)

    def profile(self, user_id, owner_identity):
        with _PRIVATE_LOCK:
            return self._read().get(_target_key(user_id, 'account-style', owner_identity), {})

    def samples(self, user_id, owner_identity, contact_id, identity):
        for contact in self.profile(user_id, owner_identity).get('contacts', []):
            if contact['contact_id']==contact_id and contact['identity']==identity:
                return contact['samples']
        return []

    def features(self, user_id, owner_identity):
        texts=[s for c in self.profile(user_id,owner_identity).get('contacts',[]) for s in c['samples']]
        if not texts:
            return {}
        # Aggregate form only. Never send another contact's text or personal facts.
        count=len(texts)
        return {'source':'human_before_first_ai_attempt', 'sample_count':count,
                'median_characters':statistics.median(map(len,texts)),
                'question_ratio':round(sum(s.endswith(('?','？')) for s in texts)/count,2),
                'exclamation_ratio':round(sum(s.endswith(('!','！')) for s in texts)/count,2),
                'terminal_punctuation_ratio':round(sum(s[-1] in '。！？!?.,，' for s in texts)/count,2),
                'emoji_ratio':round(sum(any(ord(ch)>0x1f000 for ch in s) for s in texts)/count,2),
                'common_endings':{ch:sum(s.endswith(ch) for s in texts) for ch in '呀哦哈啊呢嘛啦嗯'
                                  if sum(s.endswith(ch) for s in texts)>=3}}


def learning_status(data_dir, user_id, owner_identity):
    profile=StyleArchive(data_dir).profile(user_id, owner_identity)
    if not profile:
        return {}
    contacts=profile.get('contacts',[])
    return {'updated_at':profile['updated_at'], 'contact_count':len(contacts),
            'message_count':sum(c['loaded_count'] for c in contacts),
            'human_sample_count':sum(len(c['samples']) for c in contacts),
            'limited':profile['limited'], 'failed_count':profile.get('failed_count',0)}


def select_examples(samples, topic, limit=18):
    def grams(text):
        text=re.sub(r'\s','',text.lower())
        return {text[i:i+2] for i in range(len(text)-1)}
    target=grams(topic)
    unique=list(dict.fromkeys(samples))
    ranked=sorted(enumerate(unique), key=lambda x:(len(grams(x[1]) & target),x[0]), reverse=True)
    return [s for _,s in ranked[:limit]]


def reply_context(rows, pending, *, contact_name, samples, account_style, now, timezone):
    zone=ZoneInfo(timezone)
    pending_ids={str(r['server_id']) for r in pending}
    chosen=[r for i,r in enumerate(rows) if i>=len(rows)-60 or str(r['server_id']) in pending_ids]
    if len(chosen)>100:
        raise AIError('待回复上下文超过限制，请手动处理部分消息后重试。')
    messages=[]
    for r in chosen:
        text=message_text(r)
        if str(r['server_id']) in pending_ids and len(text)>4000:
            raise AIError('待回复文字过长，请手动处理后重试。')
        messages.append({'id':str(r['server_id']), 'role':'assistant' if r['from_me'] else 'user',
                         'source':'own_context_not_style' if r['from_me'] else 'contact',
                         'created_at':datetime.fromtimestamp(r['created_at']/1000, zone).isoformat(),
                         'content':text[:4000], 'truncated':len(text)>4000})
        kind=(r.get('media') or {}).get('kind')
        if r.get('message_type')!=7 or kind in ('image','sticker','video'):
            if not kind and supported_incoming(r):
                kind='sticker'
            messages[-1]['media']={'kind':kind or 'unknown','coverage':'unknown'}
    payload={'current_time':now.astimezone(zone).isoformat(), 'timezone':timezone,
             'contact':{'name':contact_name[:100], 'kind':'direct'}, 'messages':messages,
             'pending_ids':[str(r['server_id']) for r in pending],
             'style_examples':[{'content':s,'source':'human','scope':'current_contact'}
                               for s in select_examples(samples,' '.join(message_text(r) for r in pending))],
             'account_style':account_style, 'history_coverage':'bounded_webpage_history; earlier messages may be missing'}
    if len(json.dumps(payload, ensure_ascii=False))>120000:
        raise AIError('待回复上下文超过限制，请手动处理部分消息后重试。')
    return payload
