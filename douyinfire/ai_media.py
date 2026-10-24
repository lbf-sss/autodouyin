"""Pure helpers for explicitly observed incoming media and bounded model inputs.

No file, download or network access belongs here. Observations are ephemeral and
must be bound by the caller to the already verified contact's pending messages.
"""
from __future__ import annotations

import base64
import binascii
import re

_KINDS = {'image', 'sticker', 'video'}
_IMAGE_LIMIT = 2 * 1024 * 1024
_TOTAL_LIMIT = 8 * 1024 * 1024
_DATA_URL = re.compile(r'data:(image/(?:jpeg|png|gif|webp));base64,([A-Za-z0-9+/]*={0,2})\Z')


def _media_kind(row: dict) -> str:
    media = row.get('media')
    if isinstance(media, dict) and isinstance(media.get('kind'), str) and media['kind'] in _KINDS:
        return media['kind']
    expression = row.get('expression')
    if row.get('message_type') == 5 and isinstance(expression, dict) and isinstance(expression.get('name'), str) and expression['name'].strip():
        return 'sticker'
    return ''


def supported_incoming(row: dict) -> bool:
    """Recognize text or explicit media, without guessing message type enums."""
    if not isinstance(row, dict):
        return False
    return bool((row.get('message_type') == 7 and isinstance(row.get('text'), str) and row['text'].strip()) or _media_kind(row))


def message_text(row: dict) -> str:
    """Give context a readable description without claiming unseen contents."""
    if row.get('message_type') == 7 and isinstance(row.get('text'), str) and row['text'].strip():
        return row['text'].strip()
    kind = _media_kind(row)
    media = row.get('media') if isinstance(row.get('media'), dict) else {}
    title = media.get('title', '')
    expression = row.get('expression') if isinstance(row.get('expression'), dict) else {}
    if not isinstance(title, str) or not title.strip():
        title = expression.get('name', '')
    title = title.strip()[:1000] if isinstance(title, str) else ''
    label = {'image': '图片', 'sticker': '表情', 'video': '视频'}.get(kind, '非文字消息')
    return '[' + label + ('：' + title if title else '，内容未知') + ']'


def _label(value, limit: int, *, empty: bool = False) -> bool:
    return (isinstance(value, str) and len(value) <= limit and (empty or bool(value.strip()))
            and not any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value))


def validated_observations(media) -> list[dict]:
    """Validate size, local data URLs, MIME signatures and safe text labels.

    Every error is deliberately independent of private inputs.
    """
    error = '媒体内容格式不正确或超过限制。'
    if media is None:
        return []
    if not isinstance(media, list) or len(media) > 4:
        raise ValueError(error)
    images_count = total = 0
    result = []
    for observation in media:
        if not isinstance(observation, dict):
            raise ValueError(error)
        message_id, kind, text = (observation.get(k) for k in ('message_id', 'kind', 'text'))
        images = observation.get('images')
        if (not _label(message_id, 256) or not isinstance(kind, str) or kind not in _KINDS or not _label(text, 4000, empty=True)
                or not isinstance(images, list)):
            raise ValueError(error)
        images_count += len(images)
        if images_count > 12:
            raise ValueError(error)
        cleaned = []
        for image in images:
            if not isinstance(image, dict) or not _label(image.get('label'), 256):
                raise ValueError(error)
            url = image.get('data_url')
            if not isinstance(url, str) or len(url) > 4 * ((_IMAGE_LIMIT + 2) // 3) + 64:
                raise ValueError(error)
            match = _DATA_URL.fullmatch(url)
            if not match:
                raise ValueError(error)
            try:
                data = base64.b64decode(match[2], validate=True)
            except (ValueError, binascii.Error):
                raise ValueError(error) from None
            valid_magic = {
                'image/jpeg': data.startswith(b'\xff\xd8\xff'),
                'image/png': data.startswith(b'\x89PNG\r\n\x1a\n'),
                'image/gif': data.startswith((b'GIF87a', b'GIF89a')),
                'image/webp': data.startswith(b'RIFF') and data[8:12] == b'WEBP',
            }[match[1]]
            total += len(data)
            if not valid_magic or len(data) > _IMAGE_LIMIT or total > _TOTAL_LIMIT:
                raise ValueError(error)
            cleaned.append({'data_url': url, 'label': image['label']})
        result.append({'message_id': message_id, 'kind': kind, 'text': text, 'images': cleaned})
    return result
