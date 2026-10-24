from pathlib import Path

import pytest

from douyinfire.config import ContactConfig, ConfigError, config_to_dict, load_config, parse_config, save_config, stable_id
from douyinfire.identities import group_identity


def test_parse_config_applies_defaults() -> None:
    config = parse_config(
        {
            "users": [
                {
                    "name": "main",
                    "contacts": ["a"],
                    "message": "hello",
                }
            ]
        },
        base_dir=Path("/tmp/douyinfire"),
    )

    assert config.users[0].name == "main"
    assert config.schedule.time == "00:05"
    assert config.schedule.jitter_minutes == 20
    assert config.timeouts.home_ready_seconds == 5
    assert config.timeouts.contact_search_seconds == 15
    assert config.data_dir == Path("/tmp/douyinfire/data")
    assert config.users[0].contacts[0].name == "a"
    assert config.users[0].contacts[0].profile_url == ""


def test_parse_config_accepts_timeouts() -> None:
    config = parse_config(
        {
            "timeouts": {
                "home_ready_seconds": 9,
                "message_panel_seconds": 12,
                "contact_search_seconds": 30,
                "input_box_seconds": 14,
                "after_send_seconds": 4,
            },
            "users": [{"name": "main", "contacts": ["a"], "message": "hello"}],
        }
    )

    assert config.timeouts.home_ready_seconds == 9
    assert config.timeouts.message_panel_seconds == 12
    assert config.timeouts.contact_search_seconds == 30
    assert config.timeouts.input_box_seconds == 14
    assert config.timeouts.after_send_seconds == 4


def test_parse_config_accepts_browser_config() -> None:
    config = parse_config(
        {
            "browser": {
                "backend": "cloakbrowser",
                "run_headless": True,
                "login_headless": False,
                "storage_state_path": "data/states/main.json",
            },
            "users": [{"name": "main", "contacts": ["a"], "message": "hello"}],
        },
        base_dir=Path("/tmp/douyinfire"),
    )

    assert config.browser.backend == "playwright"
    assert config.migration_warnings
    assert config.browser.run_headless is True
    assert config.browser.storage_state_path == Path("/tmp/douyinfire/data/states/main.json")


def test_parse_config_accepts_profile_contact_objects() -> None:
    config = parse_config(
        {
            "users": [
                {
                    "name": "main",
                    "contacts": [
                        {
                            "name": "friend",
                            "profile_url": "https://www.douyin.com/user/example",
                            "message": "custom",
                        }
                    ],
                    "message": "hello",
                }
            ]
        }
    )

    contact = config.users[0].contacts[0]
    assert contact.name == "friend"
    assert contact.profile_url == "https://www.douyin.com/user/example"
    assert contact.message == "custom"


def test_parse_config_rejects_bad_browser_backend() -> None:
    with pytest.raises(ConfigError):
        parse_config(
            {
                "browser": {"backend": "bad"},
                "users": [{"name": "main", "contacts": ["a"], "message": "hello"}],
            }
        )






def test_parse_config_rejects_bad_timeout() -> None:
    with pytest.raises(ConfigError):
        parse_config(
            {
                "timeouts": {"contact_search_seconds": 0},
                "users": [{"name": "main", "contacts": ["a"], "message": "hello"}],
            }
        )


def test_parse_config_rejects_missing_users() -> None:
    with pytest.raises(ConfigError):
        parse_config({})


def test_parse_config_rejects_bad_time() -> None:
    with pytest.raises(ConfigError):
        parse_config(
            {
                "schedule": {"time": "25:99"},
                "users": [{"name": "main", "contacts": ["a"], "message": "hello"}],
            }
        )


def contacts_config(*contacts):
    return {"users": [{"name": "synthetic account", "contacts": list(contacts)}]}


def test_old_direct_contact_identity_and_id_are_unchanged(tmp_path):
    raw = contacts_config({"name": "synthetic friend", "identity": "old-secUid", "future_option": True})
    config = parse_config(raw)
    contact = config.users[0].contacts[0]
    assert contact.kind == "direct"
    assert contact.identity == "old-secUid"
    assert contact.id == stable_id("contact", "old-secUid")
    target = tmp_path / "config" / "app.json"
    save_config(config, target)
    loaded = load_config(target).users[0].contacts[0]
    assert (loaded.kind, loaded.identity, loaded.id) == ("direct", "old-secUid", contact.id)
    assert loaded.extra == {"future_option": True}


def test_group_and_direct_can_share_a_name_without_sharing_local_id():
    config = parse_config(contacts_config({"name": "same name"}, {"name": "same name", "kind": "group"}))
    direct, group = config.users[0].contacts
    assert direct.id == stable_id("contact", "same name")
    assert group.id != direct.id
    assert group.identity == "" and group.profile_url == ""
    assert config_to_dict(config)["users"][0]["contacts"][1]["kind"] == "group"


def test_bound_group_round_trip_keeps_exact_identity_and_explicit_id(tmp_path):
    identity = group_identity("1234567890123456789")
    config = parse_config(contacts_config({"name": "synthetic group", "kind": "group", "identity": identity, "id": "keep-existing-id"}))
    target = tmp_path / "config" / "app.yaml"
    save_config(config, target)
    contact = load_config(target).users[0].contacts[0]
    assert (contact.kind, contact.identity, contact.id) == ("group", identity, "keep-existing-id")


@pytest.mark.parametrize("values", [
    {"kind": "unknown"}, {"kind": None}, {"kind": []},
    {"kind": "group", "identity": "old-secUid"},
    {"kind": "group", "identity": "group:YQ="},
    {"kind": "group", "identity": " group:YQ"},
    {"kind": "group", "identity": 1234567890123456789},
    {"kind": "direct", "identity": group_identity("1234567890123456789")},
    {"kind": "group", "profile_url": "https://www.douyin.com/user/synthetic"},
])
def test_config_rejects_invalid_contact_type_or_identity(values):
    with pytest.raises(ConfigError):
        parse_config(contacts_config({"name": "synthetic", **values}))


def test_duplicate_group_identity_rejected_even_with_different_local_ids():
    identity = group_identity("1234567890123456789")
    with pytest.raises(ConfigError, match="contact identities"):
        parse_config(contacts_config(
            {"name": "group one", "kind": "group", "identity": identity, "id": "one"},
            {"name": "group renamed", "kind": "group", "identity": identity, "id": "two"},
        ))


def test_contact_kind_is_appended_after_legacy_positional_fields():
    contact = ContactConfig("old", "", "hello", "local-id", False, "old-secUid", {"future": 1})
    assert contact.kind == "direct" and contact.extra == {"future": 1}


def test_interactive_expression_roundtrip_and_legacy_defaults(tmp_path):
    raw = {'users': [{'name': 'main', 'message': '', 'interactive_expression': '续火花',
                     'contacts': [{'name': 'a', 'interactive_expression': '比心'}, {'name': 'b', 'message': 'hello'}]}]}
    cfg = parse_config(raw, base_dir=tmp_path)
    assert cfg.users[0].interactive_expression == '续火花'
    assert cfg.users[0].contacts[0].interactive_expression == '比心'
    assert cfg.users[0].contacts[1].interactive_expression == ''
    path = tmp_path / 'config.json'
    save_config(cfg, path)
    assert load_config(path).users[0].interactive_expression == '续火花'
    assert load_config(path).users[0].contacts[0].interactive_expression == '比心'


@pytest.mark.parametrize('expression', [True, [], 12, 'x' * 81, '比心\n发送', '  '])
def test_interactive_expression_rejects_invalid_names(expression):
    with pytest.raises(ConfigError):
        parse_config({'users': [{'name': 'main', 'message': '', 'interactive_expression': expression}]})


@pytest.mark.parametrize('at_contact', [False, True])
def test_text_and_interactive_expression_are_mutually_exclusive(at_contact):
    user = {'name': 'main', 'message': '文字', 'interactive_expression': '比心'}
    if at_contact:
        user = {'name': 'main', 'message': '默认', 'contacts': [user]}
    with pytest.raises(ConfigError, match='互动表情'):
        parse_config({'users': [user]})
