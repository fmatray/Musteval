"""L2 leaf `commands`: slash line -> frames."""

from __future__ import annotations

import base64

import pytest

from bridge.commands import (
    FS_PUT_CHUNK_BYTES,
    LOCAL_COMMANDS,
    CommandError,
    CommandState,
    parse,
)
from bridge.protocol import COMMAND_NAMES, reset_ids


@pytest.fixture(autouse=True)
def _ids():
    reset_ids()
    yield
    reset_ids()


def test_every_produced_name_is_a_frozen_command():
    texts = [
        "/status", "/reload", "/reboot", "/templates", "/template t1", "/geo",
        "/tasks", "/run t1", "/config get a.b", "/config set a.b c", "/mqtt",
        "/mqtt publish s1 t p", "/fs put /x",
        "/sandbox", "/sandbox list /workspace/runs", "/sandbox read /workspace/a.txt",
        "/sandbox delete /workspace/a.txt",
    ]
    state = CommandState(tasks=[{"id": "t1", "enabled": True}], upload_bytes=b"a")
    for text in texts:
        for frame in parse(text, state):
            if frame.type == "command":
                assert frame.body["name"] in COMMAND_NAMES


def test_status_builds_a_status_command():
    (frame,) = parse("/status")
    assert frame.type == "command"
    assert frame.body == {"name": "status", "args": {}}


def test_reload_and_reboot():
    assert parse("/reload")[0].body["name"] == "reload"
    assert parse("/reboot")[0].body["name"] == "reboot"


def test_rejects_a_non_slash_line():
    with pytest.raises(CommandError):
        parse("hello")


def test_rejects_an_empty_command():
    with pytest.raises(CommandError):
        parse("/")


def test_rejects_an_unknown_command():
    with pytest.raises(CommandError):
        parse("/nope")


def test_every_local_command_sends_nothing():
    for name in LOCAL_COMMANDS:
        assert parse(f"/{name}") == []


def test_logs_and_help_are_local_commands():
    assert {"logs", "help"} <= LOCAL_COMMANDS


def test_local_commands_do_not_collide_with_device_commands():
    assert not LOCAL_COMMANDS & {"/" + n.split(".")[0] for n in COMMAND_NAMES}


def test_unbalanced_quotes_are_a_command_error():
    with pytest.raises(CommandError):
        parse('/mqtt publish s1 t "unclosed')


def test_template_requires_a_name():
    with pytest.raises(CommandError):
        parse("/template")
    assert parse("/template morning")[0].body["args"] == {"name": "morning"}


def test_retry_resends_the_last_prompt():
    (frame,) = parse("/retry", CommandState(last_prompt="hi"))
    assert frame.type == "prompt"
    assert frame.body == {"text": "hi"}


def test_retry_without_a_prompt_is_rejected():
    with pytest.raises(CommandError):
        parse("/retry", CommandState())


def test_ping_and_cancel_are_fire_and_forget():
    assert parse("/ping")[0].type == "ping"
    cancel = parse("/cancel")[0]
    assert cancel.type == "cancel"
    assert cancel.body["reason"] == "user"


@pytest.mark.parametrize(
    "text,usage_error",
    [
        ("/config", True),
        ("/config get", True),
        ("/config set a.b", True),
        ("/config bogus a", True),
        ("/run", True),
        ("/tasks delete", True),
        ("/tasks enable", True),
        ("/tasks set", True),
        ("/tasks add", True),
        ("/tasks bogus", True),
        ("/fs", True),
        ("/fs put", True),
        ("/mqtt publish s1", True),
        ("/secret set", True),
    ],
)
def test_usage_errors(text, usage_error):
    with pytest.raises(CommandError):
        parse(text, CommandState(tasks=[{"id": "t1"}], upload_bytes=b"a"))


def test_config_get_and_set():
    assert parse("/config get wifi.ssid")[0].body["args"] == {"path": "wifi.ssid"}
    assert parse("/config set wifi.ssid home")[0].body["args"] == {
        "path": "wifi.ssid",
        "value": "home",
    }
    # A value with spaces stays one argument.
    assert parse("/config set llm.model a b c")[0].body["args"]["value"] == "a b c"


def test_mqtt_publish_builds_the_three_arguments():
    (frame,) = parse("/mqtt publish srv1 home/temp 21.5")
    assert frame.body["name"] == "mqtt.publish"
    assert frame.body["args"] == {"server_id": "srv1", "topic": "home/temp", "payload": "21.5"}


def test_mqtt_alone_asks_for_status():
    assert parse("/mqtt")[0].body["name"] == "mqtt.status"


def test_tasks_list():
    assert parse("/tasks")[0].body["name"] == "tasks.list"


def test_tasks_enable_flips_the_cached_task():
    state = CommandState(tasks=[{"id": "t1", "description": "d", "enabled": False}])
    (frame,) = parse("/tasks enable t1", state)
    assert frame.body["name"] == "tasks.set"
    assert frame.body["args"]["task"]["enabled"] is True
    assert frame.body["args"]["task"]["description"] == "d"

    (frame,) = parse("/tasks disable t1", state)
    assert frame.body["args"]["task"]["enabled"] is False


def test_tasks_set_roundtrips_the_cached_task():
    state = CommandState(tasks=[{"id": "t1", "enabled": True}])
    assert parse("/tasks set t1", state)[0].body["args"]["task"] == {"id": "t1", "enabled": True}


def test_tasks_delete():
    assert parse("/tasks delete t1")[0].body == {"name": "tasks.delete", "args": {"id": "t1"}}


def test_unknown_task_id_is_an_error_not_a_silent_noop():
    with pytest.raises(CommandError):
        parse("/tasks enable nope", CommandState(tasks=[{"id": "t1"}]))


def test_run_targets_a_task():
    assert parse("/run t1")[0].body == {"name": "tasks.run", "args": {"id": "t1"}}


def test_secret_set_requires_a_value_in_state():
    with pytest.raises(CommandError):
        parse("/secret set wifi", CommandState())
    state = CommandState(secret_value="hunter2")
    (frame,) = parse("/secret set wifi", state)
    assert frame.type == "secret.set"
    assert frame.body == {"key": "wifi", "value": "hunter2"}


def test_fs_put_splits_under_the_rx_cap():
    data = b"x" * (FS_PUT_CHUNK_BYTES * 2 + 10)
    frames = parse("/fs put /ws/a.txt", CommandState(upload_bytes=data))
    assert len(frames) == 3
    assert [f.body["args"]["final"] for f in frames] == [False, False, True]
    assert [f.body["args"]["offset"] for f in frames] == [0, FS_PUT_CHUNK_BYTES, FS_PUT_CHUNK_BYTES * 2]
    rebuilt = b"".join(base64.b64decode(f.body["args"]["data"]) for f in frames)
    assert rebuilt == data
    assert all(f.body["args"]["encoding"] == "base64" for f in frames)


def test_fs_put_requires_an_absolute_path():
    with pytest.raises(CommandError):
        parse("/fs put rel/path", CommandState(upload_bytes=b"a"))


def test_fs_put_without_a_loaded_file():
    with pytest.raises(CommandError):
        parse("/fs put /a", CommandState())


def test_fs_put_of_an_empty_file_still_sends_one_final_chunk():
    (frame,) = parse("/fs put /empty", CommandState(upload_bytes=b""))
    assert frame.body["args"]["final"] is True
    assert frame.body["args"]["data"] == ""


# -- sandbox (`04` §4.17-§4.20, `05` §5.8) ------------------------------


def test_bare_sandbox_lists_the_root():
    (frame,) = parse("/sandbox")
    assert frame.body["name"] == "sandbox.list"
    assert frame.body["args"] == {"path": "/workspace/"}


def test_sandbox_subcommands_map_to_their_frames():
    (listing,) = parse("/sandbox list /workspace/runs")
    assert listing.body["name"] == "sandbox.list"
    assert listing.body["args"] == {"path": "/workspace/runs"}

    (body,) = parse("/sandbox read /workspace/a.txt")
    assert body.body["name"] == "sandbox.read"
    assert body.body["args"] == {"path": "/workspace/a.txt"}

    (deleted,) = parse("/sandbox delete /workspace/a.txt")
    assert deleted.body["name"] == "sandbox.delete"
    assert deleted.body["args"] == {"path": "/workspace/a.txt"}


@pytest.mark.parametrize(
    "text",
    [
        "/sandbox read /etc/passwd",
        "/sandbox delete /workspace/../config.json",
        "/sandbox list /fs/config.json",
        "/sandbox read relative.txt",
    ],
)
def test_a_sandbox_path_outside_the_mount_is_refused_before_sending(text):
    """The device is the authority, but a refused path should cost no round trip."""
    with pytest.raises(CommandError):
        parse(text)


def test_an_unknown_sandbox_subcommand_is_an_error():
    with pytest.raises(CommandError):
        parse("/sandbox execute /workspace/a.txt")
