from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import Mock
import pytest
from douyinfire.config import parse_config, save_config
from douyinfire import ai_tasks, ai_core
from douyinfire.ai_core import PrivateAIStore, ReplyLedger


def message(number, own=False, **kw):
    return dict(server_id=str(number),client_id=str(number),created_at=number*1000,from_me=own,
                message_type=7,text='测试',sender='owner' if own else 'friend',**kw)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path=tmp_path/'config'/'config.yaml'
    raw={'users':[{'id':'u','name':'我','identity':'owner','message':'','contacts':[
        {'id':'c','name':'好友','identity':'friend','profile_url':'https://www.douyin.com/user/friend'}]}],
        'ai_reply':{'enabled':True,'user_id':'u','contact_ids':['c'],'generation':'g'}}
    config=parse_config(raw,base_dir=tmp_path)
    save_config(config,path)
    store=PrivateAIStore(config.data_dir)
    store.set_key('sk-unit-test-only')
    store.set_samples('u','c','friend',['好呀','等下哈'])
    clock=[datetime(2026,10,20,19,5,tzinfo=ZoneInfo('Asia/Shanghai'))]
    monkeypatch.setattr(ai_tasks,'_now',lambda:clock[0])
    monkeypatch.setattr(ai_core.time,'time',lambda:100)
    from contextlib import nullcontext
    b=Mock()
    b.open_direct_history.return_value=[message(100,True)]
    b.read_direct_history.return_value=[message(100,True),message(200)]
    b.send_current_reply.side_effect=lambda identity,text,**kw:(kw['guard'](),kw['before_send'](),{'message_id':'900'})[-1]
    monkeypatch.setattr(ai_tasks,'_browser',lambda *a,**k:nullcontext(b))
    generate=Mock(return_value='好呀')
    monkeypatch.setattr(ai_tasks,'generate_reply',generate)
    ai_tasks.poll_replies(path,'u')
    generate.assert_not_called()
    b.open_direct_history.return_value=[message(100,True),message(200)]
    return path,config,b,generate,clock


def test_new_message_once_and_persistent_dedup(setup):
    path,c,b,g,clock=setup
    ai_tasks.poll_replies(path,'u')
    assert g.call_count==1
    b.send_current_reply.assert_called_once()
    ai_tasks.poll_replies(path,'u')
    assert g.call_count==1
    assert ReplyLedger(c.data_dir).history('u')[0]['status']=='confirmed'


def test_poll_supplies_timestamped_context_and_all_pending_ids(setup):
    path,c,b,g,clock=setup
    rows=[message(100,True)]+[message(n) for n in range(101,135)]
    b.open_direct_history.return_value=rows
    b.read_direct_history.return_value=rows
    ai_tasks.poll_replies(path,'u')
    payload=g.call_args.kwargs['chat_context']
    assert payload['pending_ids']==[str(n) for n in range(101,135)]
    assert payload['contact']['name']=='好友'
    assert len(payload['messages'])==35
    assert payload['timezone']=='Asia/Shanghai'
    assert payload['messages'][-1]['created_at'].endswith('+08:00')


def test_contact_without_own_samples_uses_account_style_instead_of_skipping(setup):
    from douyinfire.ai_style import StyleArchive
    path,c,b,g,clock=setup
    PrivateAIStore(c.data_dir).set_samples('u','c','friend',[])
    StyleArchive(c.data_dir).save('u','owner',[
        {'contact_id':'other','identity':'other-friend','samples':['不能把其他人的私事传过去'], 'loaded_count':1}])
    ai_tasks.poll_replies(path,'u')
    g.assert_called_once()
    payload=g.call_args.kwargs['chat_context']
    assert payload['style_examples']==[]
    assert payload['account_style']['sample_count']==1
    assert '不能把其他人的私事传过去' not in str(payload)
    b.send_current_reply.assert_called_once()


def test_learn_is_read_only_preserves_targets_and_excludes_ai(setup):
    path,c,b,g,clock=setup
    from douyinfire.ai_style import StyleArchive
    rows=[message(1,True),message(2),message(999999999999,True)]
    b.collect_direct_history.return_value={'rows':rows,'stop_reason':'limit'}
    # Existing failed attempt also marks the conservative start of AI activity.
    ReplyLedger(c.data_dir).reserve('u','friend','200','c')
    before=c.ai_reply
    result=ai_tasks.learn_account_style(c,c.user('u'))
    assert result['contact_count']==1 and result['human_sample_count']==1
    assert c.ai_reply==before
    assert StyleArchive(c.data_dir).samples('u','owner','c','friend')==['测试']
    b.send_current_reply.assert_not_called()


def test_relearning_keeps_prior_verified_samples_when_webpage_loads_less(setup):
    from douyinfire.ai_style import StyleArchive
    path,c,b,g,clock=setup
    a=StyleArchive(c.data_dir)
    a.save('u','owner',[{'contact_id':'c','identity':'friend','samples':['先前本人文字'],'loaded_count':40}])
    b.collect_direct_history.return_value={'rows':[message(1,True)],'stop_reason':'unverified_page'}
    result=ai_tasks.learn_account_style(c,c.user('u'))
    assert result['message_count']==40
    assert a.samples('u','owner','c','friend')==['先前本人文字','测试']


def test_manual_reply_skips_generation(setup):
    path,c,b,g,clock=setup
    b.open_direct_history.return_value += [message(201,True)]
    ai_tasks.poll_replies(path,'u')
    g.assert_not_called()


def test_late_mount_of_older_history_does_not_cancel_valid_reply(setup):
    path,c,b,g,clock=setup
    b.read_direct_history.return_value=[message(50),message(100,True),message(200)]
    ai_tasks.poll_replies(path,'u')
    b.send_current_reply.assert_called_once()
    assert ReplyLedger(c.data_dir).history('u')[0]['status']=='confirmed'


def test_each_contact_opens_fresh_browser_to_avoid_stale_empty_chat(setup,monkeypatch):
    from contextlib import nullcontext
    path,c,b,g,clock=setup
    user=c.user('u')
    second=replace(user.contacts[0],id='second',name='second',identity='friend-two',
                   profile_url='https://www.douyin.com/user/friend-two')
    user.contacts.append(second)
    c.ai_reply.contact_ids.append('second')
    save_config(c,path)
    factory=Mock(side_effect=lambda *a,**kw:nullcontext(b))
    monkeypatch.setattr(ai_tasks,'_browser',factory)
    ai_tasks.poll_replies(path,'u')
    assert factory.call_count==2


def test_history_only_contact_samples_are_own_texts(tmp_path,monkeypatch):
    from contextlib import nullcontext
    c=parse_config({'users':[{'id':'u','name':'我','identity':'owner','message':'','contacts':[
        {'id':'c','name':'好友','identity':'friend','profile_url':'https://www.douyin.com/user/friend'}]}]},base_dir=tmp_path)
    b=Mock()
    b.open_direct_history.return_value=[message(1),message(2,True),message(3,True)]
    monkeypatch.setattr(ai_tasks,'_browser',lambda *a,**k:nullcontext(b))
    result=ai_tasks.import_samples(c,c.user('u'),'c')
    assert result['sample_count']==1
    assert PrivateAIStore(c.data_dir).samples('u','c','friend')==['测试']
    b.send_current_reply.assert_not_called()


@pytest.mark.parametrize('change',['window','pause','incoming','manual','cancel'])
def test_generation_race_never_dispatches(setup,change):
    path,c,b,g,clock=setup
    stopped=[False]
    def generate(*a,**k):
        if change=='window': clock[0]=clock[0].replace(hour=21,minute=0)
        if change=='pause':
            c.ai_reply.enabled=False
            save_config(c,path)
        if change=='incoming': b.read_direct_history.return_value += [message(201)]
        if change=='manual': b.read_direct_history.return_value += [message(201,True)]
        if change=='cancel': stopped[0]=True
        return '好呀'
    g.side_effect=generate
    ai_tasks.poll_replies(path,'u',should_stop=lambda:stopped[0])
    statuses=[r['status'] for r in ReplyLedger(c.data_dir).history('u')]
    assert 'dispatching' not in statuses and 'confirmed' not in statuses


def test_uncertain_reply_blocks_future_poll(setup):
    from douyinfire.browser import SendUncertain
    path,c,b,g,clock=setup
    def uncertain(*a,**kw):
        kw['before_send']()
        raise SendUncertain('uncertain')
    b.send_current_reply.side_effect=uncertain
    ai_tasks.poll_replies(path,'u')
    b.open_direct_history.return_value += [message(300)]
    ai_tasks.poll_replies(path,'u')
    assert g.call_count==1
    assert ReplyLedger(c.data_dir).blocked('u','friend')


def test_first_poll_answers_outstanding_text_even_previously_baselined(setup):
    path,c,b,g,clock=setup
    ledger=ReplyLedger(c.data_dir)
    rows=[message(100,True),message(150),message(200)]
    ledger.baseline('old-activation','u','friend',rows)
    c.ai_reply.generation='changed-settings'
    save_config(c,path)
    b.open_direct_history.return_value=rows
    b.read_direct_history.return_value=rows
    ai_tasks.poll_replies(path,'u')
    g.assert_called_once()
    b.send_current_reply.assert_called_once()


def test_failed_generation_retries_still_unanswered_message(setup):
    path,c,b,g,clock=setup
    g.side_effect=[ai_core.AIError('模型暂时不可用'),'好呀']
    ai_tasks.poll_replies(path,'u')
    b.send_current_reply.assert_not_called()
    ai_tasks.poll_replies(path,'u')
    b.send_current_reply.assert_called_once()
    assert g.call_count==2


def test_manual_or_new_message_race_is_reconsidered_next_poll(setup):
    path,c,b,g,clock=setup
    def first(*a,**kw):
        b.read_direct_history.return_value += [message(250)]
        return '好呀'
    g.side_effect=first
    ai_tasks.poll_replies(path,'u')
    b.send_current_reply.assert_not_called()
    b.open_direct_history.return_value=b.read_direct_history.return_value
    g.side_effect=None
    ai_tasks.poll_replies(path,'u')
    b.send_current_reply.assert_called_once()


def test_pending_text_before_trailing_expression_is_answered(setup):
    path,c,b,g,clock=setup
    expression=message(250);expression.update(message_type=5,text='')
    rows=[message(100,True),message(200),expression]
    b.open_direct_history.return_value=rows
    b.read_direct_history.return_value=rows
    ai_tasks.poll_replies(path,'u')
    b.send_current_reply.assert_called_once()


def test_renewal_expression_is_not_a_reply_to_pending_text(setup):
    path,c,b,g,clock=setup
    renewal=message(250,True);renewal.update(message_type=5,text='',expression={'name':'续火花'})
    rows=[message(100,True),message(200),renewal]
    b.open_direct_history.return_value=rows
    b.read_direct_history.return_value=rows
    ai_tasks.poll_replies(path,'u')
    b.send_current_reply.assert_called_once()


def test_confirmed_message_is_not_repeated_after_settings_change(setup):
    path,c,b,g,clock=setup
    ai_tasks.poll_replies(path,'u')
    c.ai_reply.generation='new-settings'
    save_config(c,path)
    ai_tasks.poll_replies(path,'u')
    assert b.send_current_reply.call_count==1


def test_coalesced_inputs_not_repeated_when_later_history_is_partial(setup):
    path,c,b,g,clock=setup
    rows=[message(100,True),message(150),message(200)]
    b.open_direct_history.return_value=rows
    b.read_direct_history.return_value=rows
    ai_tasks.poll_replies(path,'u')
    b.open_direct_history.return_value=rows[:2]
    b.read_direct_history.return_value=rows[:2]
    ai_tasks.poll_replies(path,'u')
    assert b.send_current_reply.call_count==1


def media_message(number, kind, own=False, **media):
    row=message(number, own)
    row.update(message_type={'image':2,'video':8,'sticker':5}[kind], text='',
               media={'kind':kind, **media})
    return row


@pytest.mark.parametrize('kind',['image','video','sticker'])
def test_media_only_pending_is_captured_and_covered_once(setup,kind):
    path,c,b,g,clock=setup
    pending=media_message(200,kind,title='朋友分享')
    rows=[message(100,True),pending]
    observations=[{'message_id':'200','kind':kind,'text':'内容无法访问，未知','images':[]}]
    b.open_direct_history.return_value=rows
    b.read_direct_history.return_value=rows
    b.prepare_incoming_media.return_value=observations
    ai_tasks.poll_replies(path,'u')
    b.prepare_incoming_media.assert_called_once_with('friend',[pending])
    assert g.call_args.kwargs['media']==observations
    assert g.call_args.kwargs['chat_context']['pending_ids']==['200']
    assert g.call_args.args[2][-1]['content']
    b.send_current_reply.assert_called_once()
    ai_tasks.poll_replies(path,'u')
    assert g.call_count==1
    assert ReplyLedger(c.data_dir).history('u')[0]['status']=='confirmed'


def test_pure_text_does_not_capture_media_or_change_model_call(setup):
    path,c,b,g,clock=setup
    ai_tasks.poll_replies(path,'u')
    b.prepare_incoming_media.assert_not_called()
    assert 'media' not in g.call_args.kwargs


def test_media_batch_preserves_all_pending_and_renewal_is_not_a_reply(setup):
    path,c,b,g,clock=setup
    renewal=message(250,True)
    renewal.update(message_type=5,text='',expression={'name':'续火花'})
    pending=[message(150),media_message(200,'image'),media_message(210,'video'),
             media_message(220,'sticker')]
    rows=[message(100,True),*pending,renewal]
    b.open_direct_history.return_value=rows
    b.read_direct_history.return_value=rows
    b.prepare_incoming_media.return_value=[{'message_id':r['server_id'],'kind':r['media']['kind'],
        'text':'未知','images':[]} for r in pending[1:]]
    ai_tasks.poll_replies(path,'u')
    assert g.call_args.kwargs['chat_context']['pending_ids']==['150','200','210','220']
    b.send_current_reply.assert_called_once()


def test_more_than_four_media_does_not_generate_send_or_cover(setup):
    path,c,b,g,clock=setup
    rows=[message(100,True)]+[media_message(n,'image') for n in range(200,205)]
    b.open_direct_history.return_value=rows
    b.read_direct_history.return_value=rows
    result=ai_tasks.poll_replies(path,'u')
    g.assert_not_called()
    b.send_current_reply.assert_not_called()
    assert result['skipped']==1
    assert not any(r['status']=='confirmed' for r in ReplyLedger(c.data_dir).history('u'))


@pytest.mark.parametrize('change',['incoming','manual','media_title','pause'])
def test_capture_race_cancels_before_generation(setup,change):
    path,c,b,g,clock=setup
    rows=[message(100,True),media_message(200,'video',title='原视频',video_id='v1')]
    b.open_direct_history.return_value=rows
    b.read_direct_history.return_value=rows
    def capture(*args):
        current=[dict(r) for r in rows]
        if change=='incoming': current.append(message(201))
        if change=='manual': current.append(message(201,True))
        if change=='media_title': current[-1]={**current[-1],'media':{'kind':'video','title':'新视频','video_id':'v1'}}
        if change=='pause':
            c.ai_reply.enabled=False
            save_config(c,path)
        b.read_direct_history.return_value=current
        return [{'message_id':'200','kind':'video','text':'未知','images':[]}]
    b.prepare_incoming_media.side_effect=capture
    ai_tasks.poll_replies(path,'u')
    b.prepare_incoming_media.assert_called_once()
    g.assert_not_called()
    b.send_current_reply.assert_not_called()


def test_capture_missing_observation_does_not_cover_unseen_media(setup):
    path,c,b,g,clock=setup
    rows=[message(100,True),media_message(200,'image'),media_message(210,'video')]
    b.open_direct_history.return_value=rows
    b.read_direct_history.return_value=rows
    b.prepare_incoming_media.return_value=[{'message_id':'200','kind':'image','text':'未知','images':[]}]
    ai_tasks.poll_replies(path,'u')
    g.assert_not_called()
    b.send_current_reply.assert_not_called()


def test_history_fingerprint_tracks_media_identity_and_ignores_signed_url_churn():
    original=[media_message(200,'video',video_id='v1',title='原视频',url='https://a/?sig=old')]
    current=[media_message(200,'video',video_id='v1',title='原视频',url='https://a/?sig=new')]
    assert ai_tasks._same_current_history(original,current)
    current[0]['media']['video_id']='v2'
    assert not ai_tasks._same_current_history(original,current)


def test_typed_media_is_captured_even_if_sdk_type_is_text(setup):
    path,c,b,g,clock=setup
    pending=media_message(200,'image')
    pending['message_type']=7
    rows=[message(100,True),pending]
    b.open_direct_history.return_value=rows
    b.read_direct_history.return_value=rows
    b.prepare_incoming_media.return_value=[{'message_id':'200','kind':'image','text':'未知','images':[]}]
    ai_tasks.poll_replies(path,'u')
    b.prepare_incoming_media.assert_called_once_with('friend',[pending])
    b.send_current_reply.assert_called_once()


def test_known_expression_without_typed_metadata_is_captured(setup):
    path,c,b,g,clock=setup
    pending=message(200)
    pending.update(message_type=5,text='',expression={'name':'抱抱','id':'sticker1'})
    rows=[message(100,True),pending]
    b.open_direct_history.return_value=rows
    b.read_direct_history.return_value=rows
    b.prepare_incoming_media.return_value=[{'message_id':'200','kind':'sticker','text':'表情名：抱抱','images':[]}]
    ai_tasks.poll_replies(path,'u')
    b.prepare_incoming_media.assert_called_once_with('friend',[pending])
    b.send_current_reply.assert_called_once()
