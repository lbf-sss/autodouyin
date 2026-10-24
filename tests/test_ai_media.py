import base64

import pytest

from douyinfire import ai_media


def data_url(data=b'\x89PNG\r\n\x1a\nimage', mime='image/png'):
    return 'data:' + mime + ';base64,' + base64.b64encode(data).decode()


def observation(**values):
    return {'message_id': '42', 'kind': 'image', 'text': '图片可见',
            'images': [{'data_url': data_url(), 'label': '原图'}], **values}


@pytest.mark.parametrize('row,expected', [
    ({'message_type': 7, 'text': ' hello '}, True),
    ({'message_type': 7, 'text': ' '}, False),
    ({'message_type': 999, 'media': {'kind': 'image'}}, True),
    ({'media': {'kind': 'sticker'}}, True),
    ({'media': {'kind': 'video', 'title': '风景'}}, True),
    ({'message_type': 5, 'expression': {'name': '你好'}}, True),
    ({'message_type': 99, 'expression': {'name': '未知'}}, False),
    ({'message_type': 99}, False),
])
def test_supported_rows_are_explicit(row, expected):
    assert ai_media.supported_incoming(row) is expected


def test_message_text_preserves_text_and_names_unavailable_media():
    assert ai_media.message_text({'message_type': 7, 'text': ' hello '}) == 'hello'
    assert '风景' in ai_media.message_text({'media': {'kind': 'video', 'title': '风景'}})
    assert '你好' in ai_media.message_text({'message_type': 5, 'expression': {'name': '你好'}})
    assert ai_media.message_text({'media': {'kind': 'image'}})


def test_validated_observations_preserves_media_and_supported_magic():
    for mime, data in [('image/png', b'\x89PNG\r\n\x1a\nrest'), ('image/jpeg', b'\xff\xd8\xffrest'),
                       ('image/gif', b'GIF89arest'), ('image/webp', b'RIFF1234WEBPrest')]:
        value = observation(images=[{'label': '截图', 'data_url': data_url(data, mime)}])
        assert ai_media.validated_observations([value]) == [value]


@pytest.mark.parametrize('media', [
    [observation()] * 5,
    [observation(images=[{'label': 'x', 'data_url': 'https://private.invalid/image'}])],
    [observation(images=[{'label': 'x', 'data_url': data_url(b'bad')}])],
    [observation(images=[{'label': 'x\nInjected', 'data_url': data_url()}])],
    [observation(images=[{'label': 'x', 'data_url': data_url(b'\x89PNG\r\n\x1a\n' + b'x' * (2*1024*1024))}])],
    [observation(images=[{'label': 'x', 'data_url': data_url()}]*13)],
    [observation(images=[{'label': 'x', 'data_url': data_url(b'\x89PNG\r\n\x1a\n'+b'x'*(2*1024*1024-8))}]*3)]*2,
    [observation(kind='audio')],
])
def test_invalid_or_unbounded_media_rejected_without_echo(media):
    with pytest.raises(ValueError) as caught:
        ai_media.validated_observations(media)
    assert 'private.invalid' not in str(caught.value)
    assert 'base64' not in str(caught.value)


@pytest.mark.parametrize('media', [
    [observation(kind=[])], [observation(images='not-list')], [observation(message_id='42\x7f')],
    [observation(images=[{'label':'x', 'data_url':'data:image/png;base64,@@@'}])],
    [observation(images=[{'label':'x', 'data_url':data_url(b'\x89PNG\r\n\x1a\nrest', 'image/jpeg')}])],
])
def test_malformed_media_raises_sanitized_value_error(media):
    with pytest.raises(ValueError, match='媒体内容格式'):
        ai_media.validated_observations(media)


def test_malformed_kind_is_not_supported():
    assert not ai_media.supported_incoming({'media': {'kind': []}})
