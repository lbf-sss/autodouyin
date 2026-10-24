from unittest.mock import Mock
import pytest
from douyinfire.browser import DouyinBrowser, BrowserError


def row(n):
    return dict(server_id=str(n),created_at=n*1000,from_me=False,message_type=7,text='synthetic')


def test_history_waits_past_initial_empty_hydration():
    b=object.__new__(DouyinBrowser)
    b.read_direct_history=Mock(side_effect=[[],[],[row(1)],[row(1)],[row(1)]])
    b._pause=Mock()
    assert b.settled_direct_history('friend')==[row(1)]
    assert b._pause.call_count>=3


def test_rapidly_changing_history_fails_without_sending():
    b=object.__new__(DouyinBrowser)
    b.read_direct_history=Mock(side_effect=[[row(n)] for n in range(30)])
    b._pause=Mock()
    with pytest.raises(BrowserError):
        b.settled_direct_history('friend')
