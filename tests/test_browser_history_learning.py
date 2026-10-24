from unittest.mock import Mock
import pytest
from douyinfire.browser import DouyinBrowser, BrowserError, BrowserInterrupted, UnsupportedPage


def row(n):
    return dict(server_id=str(n),created_at=n*1000)


def reader():
    b=object.__new__(DouyinBrowser)
    b._check_interrupted=Mock()
    b._pause=Mock()
    b.open_direct_history=Mock(return_value=[row(8),row(9)])
    b.read_direct_history=Mock(return_value=[row(6),row(8)])
    page=Mock()
    page.locator.return_value.count.return_value=1
    page.locator.return_value.is_visible.return_value=True
    b._require_page=Mock(return_value=page)
    return b,page


def test_bounded_scrolling_deduplicates_and_reports_partial_coverage():
    b,page=reader()
    result=b.collect_direct_history('profile','friend',max_pages=3)
    assert [r['server_id'] for r in result['rows']]==['6','8','9']
    assert result['stop_reason']=='page_limit' and result['pages']==3
    assert page.locator.return_value.evaluate.call_count==2
    assert b.read_direct_history.call_count==4


def test_identity_change_stops_before_further_scroll():
    b,page=reader()
    b.read_direct_history.side_effect=BrowserError('changed')
    with pytest.raises(BrowserError):
        b.collect_direct_history('profile','friend')
    page.locator.return_value.evaluate.assert_not_called()


def test_history_cancellation_does_not_continue():
    b,page=reader()
    b._pause.side_effect=BrowserInterrupted('cancelled')
    with pytest.raises(BrowserInterrupted):
        b.collect_direct_history('profile','friend')
    assert page.locator.return_value.evaluate.call_count==1


def test_unsupported_scroll_surface_reports_mounted_only():
    b,page=reader()
    page.locator.return_value.count.return_value=0
    assert b.collect_direct_history('profile','friend')['stop_reason']=='mounted_only'
    b.read_direct_history.assert_not_called()


def test_unverifiable_older_page_keeps_only_already_verified_history():
    b,page=reader()
    b.read_direct_history.side_effect=[[], UnsupportedPage('old unsupported message')]
    result=b.collect_direct_history('profile','friend')
    assert result['rows']==[row(8),row(9)]
    assert result['stop_reason']=='unverified_page'
