import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from douyinfire import ai_style
from douyinfire.ai_core import AIError


def row(n, text, own=False):
    return dict(server_id=str(n), created_at=1700000000000+n, from_me=own,
                message_type=7, text=text)


def test_all_pending_questions_survive_recent_context_limit():
    rows = [row(n, f'问题{n}') for n in range(90)]
    payload = ai_style.reply_context(rows, rows, contact_name='好友', samples=['好呀'],
        account_style={}, now=datetime(2026, 10, 20, 20, tzinfo=ZoneInfo('Asia/Shanghai')),
        timezone='Asia/Shanghai')
    assert len(payload['messages']) == 90
    assert payload['pending_ids'] == [str(n) for n in range(90)]
    assert payload['current_time'].endswith('+08:00')
    assert payload['messages'][0]['created_at'].endswith('+08:00')
    assert payload['style_examples'][0]['source'] == 'human'


def test_oversize_pending_is_rejected_instead_of_covered_unseen():
    with pytest.raises(AIError):
        ai_style.reply_context([row(1, '字'*5000)], [row(1, '字'*5000)],
            contact_name='好友', samples=['好呀'], account_style={},
            now=datetime.now().astimezone(), timezone='Asia/Shanghai')


def test_learning_provenance_binding_and_no_other_contact_facts(tmp_path):
    archive = ai_style.StyleArchive(tmp_path)
    archive.save('u', 'owner', [
        {'contact_id':'a', 'identity':'friend-a', 'samples':['周六上海见呀', '好呀'], 'loaded_count':8},
        {'contact_id':'b', 'identity':'friend-b', 'samples':['我在北京上学哦'], 'loaded_count':6}], limited=True)
    assert archive.samples('u','owner','a','friend-a') == ['周六上海见呀','好呀']
    assert archive.samples('u','other-owner','a','friend-a') == []
    assert archive.samples('u','owner','a','rebound') == []
    status = ai_style.learning_status(tmp_path,'u','owner')
    assert status['human_sample_count']==3 and status['message_count']==14
    assert '北京' not in json.dumps(status, ensure_ascii=False)
    assert '北京' not in json.dumps(archive.features('u','owner'), ensure_ascii=False)
    assert archive.path.stat().st_mode & 0o777 == 0o600


def test_human_samples_exclude_incoming_and_everything_since_first_ai_attempt():
    rows=[row(1,'human',True),row(2,'friend'),row(3,'ai',True),row(4,'ambiguous',True)]
    assert ai_style.human_samples(rows, cutoff=rows[2]['created_at']) == ['human']


def test_examples_prefer_topic_without_copying_other_contact_text():
    samples=['嗯嗯','周六几点呀','好呀','周六见哦']
    result=ai_style.select_examples(samples, '周六几点见')
    assert set(result)==set(samples)
    assert '周六' in result[0]


def test_media_context_preserves_kind_unknown_coverage_without_signed_urls():
    pending=[]
    for n,kind in enumerate(['image','sticker','video'],1):
        item=row(n,'')
        item.update(message_type=5 if kind=='sticker' else 2,
                    media={'kind':kind,'title':'分享标题','url':'https://private/?signature=secret'})
        pending.append(item)
    payload=ai_style.reply_context(pending,pending,contact_name='好友',samples=['分享呀'],
        account_style={},now=datetime.now().astimezone(),timezone='Asia/Shanghai')
    assert [m['media']['kind'] for m in payload['messages']]==['image','sticker','video']
    assert all(m['media']['coverage']=='unknown' for m in payload['messages'])
    assert all(m['content'] for m in payload['messages'])
    assert 'signature' not in json.dumps(payload)
    assert payload['pending_ids']==['1','2','3']
