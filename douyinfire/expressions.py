"""Read rendered built-in interactive expressions; preparation never submits.

Selectors and metadata are from the official @pc-im/im 1.0.0.1016 bundle.
Only the caller's durable send boundary may click PreparedExpression.target.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


PANEL = '.componentsemojiemojiPanel'
ITEM = '.emojiEmojiItememojiItem'
TOGGLE = "[data-e2e='im-dialog'] [data-e2e='msg-input'] .messageMsgInputinputAction > svg.messageMsgInputiconAction"
TAB = '.emojiEmojisModalTabsubTab'
INTERACTIVE_TAB = """e => {
    const key = Object.keys(e).find(k => k.startsWith('__reactFiber'));
    // Official EmojiPage enum: Interactive=0, Small=1, Custom=2, Gif=3.
    // Each rendered tab is keyed by that enum, independently of SVG shape.
    return e[key]?.key === '0';
}"""
CLICK_READY = """e => {
    const key = Object.keys(e).find(k => k.startsWith('__reactProps'));
    return typeof e[key]?.onClick === 'function';
}"""
READ_EXPRESSION = """element => {
    const labels = element.querySelectorAll('.emojiEmojiItememojiItemDesc');
    if (labels.length !== 1) return null;
    const label = labels[0].innerText;
    const key = Object.keys(element).find(k => k.startsWith('__reactFiber'));
    let fiber = element[key];
    // Only read props of the rendered item, never the account's global store.
    for (let depth = 0; fiber && depth < 16; depth++, fiber = fiber.return) {
        const p = fiber.memoizedProps, s = p?.sticker;
        if (!s || p.needDesc !== true || p.desc !== label || p.imageId !== label) continue;
        // The official ordinary-expression sender uses image_id: s.id ?? 0.
        // Built-ins such as 续火花 omit id; their name and exact resource URI
        // still distinguish them, and both must match the acknowledged receipt.
        const id = s.id ?? 0;
        const validId = typeof id === 'string' ? /^(0|[1-9][0-9]*)$/.test(id)
            : Number.isSafeInteger(id) && id >= 0;
        if (!validId || s.display_name !== label || typeof s.static_url !== 'string'
            || !s.static_url.startsWith('https://') || !(s.width > 0 && s.height > 0)) return null;
        return {name: label, image_id: String(id), uri: s.static_url};
    }
    return null;
}"""


@dataclass
class PreparedExpression:
    item: Any
    target: Any
    identity: dict[str, str]

    def unchanged(self) -> bool:
        return (self.item.is_visible() and self.target.is_visible()
                and self.item.evaluate(READ_EXPRESSION) == self.identity
                and self.target.evaluate(CLICK_READY) is True)


def prepare_expression(page: Any, name: str, unique: Callable, check_interrupted: Callable) -> PreparedExpression:
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ValueError('请输入有效的互动表情名称。')
    if name in {'摇骰子', '猜拳'}:
        raise ValueError('暂不支持带随机结果的互动表情，请选择续火花、比心等表情。')
    panel = unique(page.locator(PANEL))
    if panel is None:
        toggle = unique(page.locator(TOGGLE))
        if toggle is None or toggle.evaluate(CLICK_READY) is not True:
            raise ValueError('没有找到可靠的抖音表情面板入口，请重新检查连接。')
        check_interrupted()
        toggle.click(timeout=1500)
        page.locator(PANEL).wait_for(state='visible', timeout=5000)
        panel = unique(page.locator(PANEL))
    if panel is None:
        raise ValueError('抖音表情面板不唯一，已停止发送。')
    tabs = [tab for tab in panel.locator(TAB).all()
            if tab.is_visible() and tab.evaluate(INTERACTIVE_TAB) is True]
    tab = tabs[0] if len(tabs) == 1 else None
    if tab is None or tab.evaluate(CLICK_READY) is not True or 'disabled' in (tab.get_attribute('class') or '').lower():
        raise ValueError('当前会话没有可用的互动表情分类。')
    check_interrupted()
    tab.click(timeout=1500)
    # Resources load asynchronously on first use. Wait only for rendered items.
    page.locator(PANEL + ' ' + ITEM).first.wait_for(state='visible', timeout=5000)
    # Read each item's own label; require an exact, unique match.
    candidates = []
    for item in page.locator(PANEL + ' ' + ITEM).all():
        labels = item.locator('.emojiEmojiItememojiItemDesc')
        if item.is_visible() and labels.count() == 1 and labels.inner_text() == name:
            candidates.append(item)
    if len(candidates) != 1:
        raise ValueError(f'互动表情“{name}”未找到或存在同名项，请核对抖音面板中的名称。')
    item = candidates[0]
    identity = item.evaluate(READ_EXPRESSION)
    target = unique(item.locator('.emojiEmojiItemimgBox[data-apm-action="EmojiItem"]'))
    if not identity or target is None or target.evaluate(CLICK_READY) is not True:
        raise ValueError('无法可靠核对互动表情身份，已停止发送。')
    check_interrupted()
    return PreparedExpression(item, target, identity)
