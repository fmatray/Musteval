"""L2 leaf `commands`: slash line -> frames."""

from __future__ import annotations

import base64

import pytest

from bridge.commands import (
    FS_PUT_CHUNK_BYTES,
    LOCAL_COMMANDS,
    TODO_OPS,
    WIFI_VERBS,
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
        "/sys status", "/sys reload", "/sys reboot", "/templates", "/template t1", "/geo",
        "/tasks", "/run t1", "/config get a.b", "/config set a.b c", "/mqtt",
        "/mqtt publish s1 t p", "/fs put /skills/a.md",
        "/sandbox", "/sandbox list /workspace/runs", "/sandbox read /workspace/a.txt",
        "/sandbox delete /workspace/a.txt",
    ]
    state = CommandState(tasks=[{"id": "t1", "enabled": True}], upload_bytes=b"a")
    for text in texts:
        for frame in parse(text, state):
            if frame.type == "command":
                assert frame.body["name"] in COMMAND_NAMES


def test_sys_status_builds_a_status_command():
    (frame,) = parse("/sys status")
    assert frame.type == "command"
    assert frame.body == {"name": "status", "args": {}}


def test_sys_reload_and_reboot():
    assert parse("/sys reload")[0].body["name"] == "reload"
    assert parse("/sys reboot")[0].body["name"] == "reboot"


def test_the_four_former_top_level_commands_are_gone():
    """`05` §7: one head owns them now, so the old spellings are unknown commands."""
    for text in ("/status", "/reload", "/reboot", "/diag"):
        with pytest.raises(CommandError) as excinfo:
            parse(text)
        assert "unknown command" in str(excinfo.value)


def test_a_sys_verb_that_is_not_a_verb_is_a_usage_error_naming_the_forms():
    with pytest.raises(CommandError) as excinfo:
        parse("/sys")
    assert str(excinfo.value) == "usage: /sys status|diag|reload|reboot|reset-counter|prompt"
    with pytest.raises(CommandError) as excinfo:
        parse("/sys restart")
    assert str(excinfo.value) == "usage: /sys status|diag|reload|reboot|reset-counter|prompt"


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
    assert parse("/ping device")[0].type == "ping"
    assert parse("/ping router")[0].body == {"name": "net.ping", "args": {"target": "router"}}
    cancel = parse("/cancel")[0]
    assert cancel.type == "cancel"
    assert cancel.body["reason"] == "user"


def test_clear_asks_the_device_to_forget_the_conversation():
    # `05` §7: the conversation lives on the device, so `/clear` must reach it. A
    # bridge-local pane wipe would leave the device history intact and the next
    # prompt would still carry the turns the reader just dismissed.
    frame = parse("/clear")[0]
    assert frame.body == {"name": "history.clear", "args": {}}


def test_clear_refuses_an_argument_it_does_not_take():
    # `04` §4.28 takes `{}`. Dropping a surplus word silently means `/clear all`
    # appears to clear everything and is answered by a plain clear.
    with pytest.raises(CommandError, match=r"usage: /clear"):
        parse("/clear all")


def test_compact_sends_the_keep_it_was_given():
    # `04` §4.33: `keep` is how many of the newest messages survive verbatim. Absent
    # means the device's default, so the frame must not carry `keep: 0` — that is a
    # value the reader did not choose, and a future default would be overridden by it.
    assert parse("/compact")[0].body == {"name": "context.compact", "args": {}}
    assert parse("/compact 2")[0].body == {
        "name": "context.compact", "args": {"keep": 2}
    }
    # Zero is a choice, and the one `/compact` already means.
    assert parse("/compact 0")[0].body == {"name": "context.compact", "args": {}}


@pytest.mark.parametrize(
    "text,message",
    [
        ("/compact 9", r"keep must be 0 to 8"),
        ("/compact -1", r"keep must be 0 to 8"),
        ("/compact two", r"keep must be a number"),
        ("/compact 1 2", r"usage: /compact"),
    ],
)
def test_compact_refuses_a_keep_it_cannot_honour(text, message):
    # `04` §4.33 caps `keep` at eight. A reader who typed nine is asking for something
    # the device would refuse, and the bridge is the layer that can say so without
    # spending a request.
    with pytest.raises(CommandError, match=message):
        parse(text)


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


def test_an_mqtt_subcommand_that_is_not_publish_is_a_usage_error():
    """`05` §6.4: only `/mqtt publish` exists, so anything else names the usage."""
    with pytest.raises(CommandError) as excinfo:
        parse("/mqtt subscribe s1 home/temp")
    assert str(excinfo.value) == "usage: /mqtt [publish <srv> <topic> <payload>]"


def test_sys_diag_is_an_alias_for_status():
    """`05` §6.4 maps `/sys diag` onto `status`, so a screen has one source."""
    (frame,) = parse("/sys diag")
    assert frame.body == {"name": "status", "args": {}}


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


def test_wifi_subcommands_map_to_their_commands():
    """`05-bridge.md` §7: `scan` asks the radio, the other four steer the station."""
    for verb in ("status", "connect", "disconnect", "reconnect"):
        (frame,) = parse(f"/wifi {verb}")
        assert frame.body["name"] == f"wifi.{verb}"


def test_wifi_scan_asks_the_radio_not_the_station():
    """`05-bridge.md` §7: `scan` is the only verb answered by the radio, so it is the
    one that can list a network the station never joined."""
    (frame,) = parse("/wifi scan")
    assert frame.body["name"] == "wifi.scan"


def test_wifi_refuses_an_unknown_verb():
    with pytest.raises(CommandError):
        parse("/wifi bogus")
    with pytest.raises(CommandError):
        parse("/wifi")


def test_todo_is_the_tools_op_set_over_the_wire():
    """`05` §7: every `/todo` verb is a `tools/todo.md` op of the same name, so the
    command cannot drift from the tool the model calls. The op set is the doc's."""
    assert TODO_OPS == (
        "init", "start", "done", "drop", "block", "unblock", "rm", "append", "view"
    )
    # Ops that can be sent bare
    for op in ("init", "done", "drop", "rm", "view"):
        (frame,) = parse(f"/todo {op}")
        assert frame.body["args"] == {"op": op}


def test_todo_bare_and_view_send_view():
    (frame,) = parse("/todo")
    assert frame.body["args"] == {"op": "view"}
    (frame,) = parse("/todo view")
    assert frame.body["args"] == {"op": "view"}


def test_todo_init_takes_a_phase_then_its_items():
    (frame,) = parse("/todo init")
    assert frame.body["args"] == {"op": "init"}
    (frame,) = parse('/todo init auth "wire jwt" "rotate key"')
    assert frame.body["args"] == {
        "op": "init",
        "phase": "auth",
        "items": ["wire jwt", "rotate key"],
    }


def test_todo_init_without_a_phase_is_refused_host_side():
    # The device refuses a phase-less `init` (`tools/todo.md` §Errors), so sending
    # one would cost a round trip to learn what the usage string already says.
    with pytest.raises(CommandError, match="init <phase> <item>"):
        parse('/todo init "wire jwt"')


def test_todo_append_takes_a_phase_then_its_items():
    (frame,) = parse('/todo append auth "wire jwt" "rotate key"')
    assert frame.body["args"] == {
        "op": "append",
        "phase": "auth",
        "items": ["wire jwt", "rotate key"],
    }


def test_todo_append_refuses_missing_phase_or_items():
    with pytest.raises(CommandError, match="phase"):
        parse("/todo append")
    with pytest.raises(CommandError, match="item"):
        parse("/todo append auth")


def test_todo_start_takes_a_task_and_refuses_none():
    (frame,) = parse("/todo start auth")
    assert frame.body["args"] == {"op": "start", "task": "auth"}
    (frame,) = parse("/todo start wire jwt")
    assert frame.body["args"] == {"op": "start", "task": "wire jwt"}
    with pytest.raises(CommandError, match="task"):
        parse("/todo start")


def test_todo_target_reaches_both_task_and_phase():
    """A target is not resolved here: `todo_apply`'s own `task → phase` chain decides,
    so `/todo done auth` finishes every task in phase `auth` with no bridge guessing."""
    for op in ("done", "drop", "rm", "block", "unblock"):
        (frame,) = parse(f"/todo {op} auth")
        assert frame.body["args"] == {"op": op, "task": "auth", "phase": "auth"}

        (frame,) = parse(f"/todo {op} wire jwt")
        assert frame.body["args"] == {"op": op, "task": "wire jwt", "phase": "wire jwt"}


def test_todo_verb_it_lacks_is_refused():
    # `expand` is a bridge-side verb the tool has no op for: sending it would be a
    # frame the device refuses, so the bridge refuses it first with a usage line.
    with pytest.raises(CommandError, match=r"usage: /todo \["):
        parse("/todo expand")


def test_todo_view_refuses_a_target_it_would_drop():
    """A trailing word on a read-only op used to be accepted and thrown away, which
    is the same shape as the bug this grammar fixes: an argument that looked
    honoured and was not."""
    with pytest.raises(CommandError, match="view takes no target"):
        parse("/todo view auth")


def test_fs_put_splits_under_the_rx_cap():
    data = b"x" * (FS_PUT_CHUNK_BYTES * 2 + 10)
    frames = parse("/fs put /skills/a.md", CommandState(upload_bytes=data))
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
    with pytest.raises(CommandError, match="no file loaded"):
        parse("/fs put /skills/a.md", CommandState())


def test_fs_put_of_an_empty_file_still_sends_one_final_chunk():
    (frame,) = parse("/fs put /skills/empty.md", CommandState(upload_bytes=b""))
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

def test_sandbox_mkdir_builds_a_command():
    (frame,) = parse("/sandbox mkdir /workspace/newdir")
    assert frame.body == {"name": "sandbox.mkdir", "args": {"path": "/workspace/newdir"}}

def test_sandbox_move_builds_a_command():
    (frame,) = parse("/sandbox move /workspace/a /workspace/b")
    assert frame.body == {"name": "sandbox.move", "args": {"from": "/workspace/a", "to": "/workspace/b"}}

def test_sandbox_delete_recursive_options():
    # Default false
    (frame,) = parse("/sandbox delete /workspace/a")
    assert frame.body == {"name": "sandbox.delete", "args": {"path": "/workspace/a"}}

    # Short flag
    (frame,) = parse("/sandbox delete -r /workspace/a")
    assert frame.body == {"name": "sandbox.delete", "args": {"path": "/workspace/a", "recursive": True}}

    # Long flag
    (frame,) = parse("/sandbox rm --recursive /workspace/a")
    assert frame.body == {"name": "sandbox.delete", "args": {"path": "/workspace/a", "recursive": True}}

    # Mix
    (frame,) = parse("/sandbox rm /workspace/a -r")
    assert frame.body == {"name": "sandbox.delete", "args": {"path": "/workspace/a", "recursive": True}}

def test_sandbox_delete_refuses_unknown_flag():
    with pytest.raises(CommandError, match="unknown option: -R"):
        parse("/sandbox rm /workspace/a -R")

def test_sandbox_mkdir_move_refuse_outside_sandbox():
    with pytest.raises(CommandError):
        parse("/sandbox mkdir /etc/passwd")
    with pytest.raises(CommandError):
        parse("/sandbox move /workspace/a /etc/passwd")

def test_sandbox_mkdir_move_missing_args():
    with pytest.raises(CommandError, match="usage: /sandbox mkdir <path>"):
        parse("/sandbox mkdir")
    with pytest.raises(CommandError, match="usage: /sandbox move <from> <to>"):
        parse("/sandbox move /workspace/a")

def test_sys_reset_counter_builds_a_command():
    (frame,) = parse("/sys reset-counter")
    assert frame.body == {"name": "sys.reset_counter", "args": {}}

def test_sys_reset_counter_rejects_bad_sub():
    """A verb outside the table names every form, rather than one of the five."""
    with pytest.raises(
        CommandError, match="usage: /sys status\\|diag\\|reload\\|reboot\\|reset-counter\\|prompt"
    ):
        parse("/sys bogus")


def _refused(text, state=None) -> str:
    """The message `parse` refuses `text` with.

    A refusal is an exception, never a short frame list: `parse` is pure, so a
    caller that got frames back would send them.
    """
    with pytest.raises(CommandError) as exc:
        parse(text, state)
    return str(exc.value)


@pytest.mark.parametrize(
    "text,expected_usage",
    [
        ("/sys status extra", "usage: /sys status"),
        ("/sys diag extra", "usage: /sys diag"),
        ("/sys reload extra", "usage: /sys reload"),
        ("/sys reboot extra", "usage: /sys reboot"),
        ("/sys reset-counter extra", "usage: /sys reset-counter"),
        ("/templates extra", "usage: /templates"),
        ("/geo extra", "usage: /geo"),
        ("/cancel extra", "usage: /cancel"),
        ("/retry extra", "usage: /retry"),
        ("/template morning junk", "usage: /template <name>"),
        ("/run t1 junk", "usage: /run <id>"),
        ("/tasks delete t1 junk", "usage: /tasks delete <id>"),
        ("/tasks enable t1 junk", "usage: /tasks enable <id>"),
        ("/tasks disable t1 junk", "usage: /tasks disable <id>"),
        ("/tasks set t1 junk", "usage: /tasks set <id>"),
        ("/sandbox list /workspace/a junk", "usage: /sandbox list [<path>]"),
        ("/sandbox read /workspace/a junk", "usage: /sandbox read <path>"),
        ("/sandbox mkdir /workspace/a junk", "usage: /sandbox mkdir <path>"),
        ("/sandbox move /workspace/a /workspace/b junk", "usage: /sandbox move <from> <to>"),
        ("/sandbox delete /workspace/a junk", "usage: /sandbox delete [-r] <path>"),
        ("/secret set key junk", "usage: /secret set <key>"),
        ("/config get a.b junk", "usage: /config get <path>"),
        ("/sys reset-counter extra", "usage: /sys reset-counter"),
        ("/fs put /workspace/a junk", "usage: /fs put <path>"),
        # The wifi verbs refuse by membership rather than `_exact`, so a surplus
        # word has to reach the same branch as an unknown verb.
        ("/wifi scan extra", "usage: /wifi " + "|".join(WIFI_VERBS)),
        ("/wifi status extra", "usage: /wifi " + "|".join(WIFI_VERBS)),
        ("/wifi connect extra", "usage: /wifi " + "|".join(WIFI_VERBS)),
        ("/wifi disconnect extra", "usage: /wifi " + "|".join(WIFI_VERBS)),
        ("/wifi reconnect extra", "usage: /wifi " + "|".join(WIFI_VERBS)),
        ("/wifi list extra", "usage: /wifi " + "|".join(WIFI_VERBS)),
        ("/wifi edit extra", "usage: /wifi " + "|".join(WIFI_VERBS)),
        ("/mqtt extra", "usage: /mqtt [publish <srv> <topic> <payload>]"),
    ],
)
def test_fixed_arity_refusals(text, expected_usage):
    # Use state with relevant data to ensure refusal is arity, not missing state
    state = CommandState(tasks=[{"id": "t1"}], upload_bytes=b"a", last_prompt="hi")
    assert _refused(text, state) == expected_usage


def test_new_slash_forms_send_zero_frames_on_refusal():
    # Proof that path refusal (and others) raises and emits nothing.
    # _refused helper already verifies this property.
    assert _refused("/sandbox mkdir /outside") == "outside /workspace/: /outside"

def test_every_produced_name_is_a_frozen_command_updated():
    texts = [
        "/sandbox mkdir /workspace/a",
        "/sandbox move /workspace/a /workspace/b",
        "/sandbox delete -r /workspace/a",
        "/sys reset-counter",
    ]
    state = CommandState()
    for text in texts:
        for frame in parse(text, state):
            if frame.type == "command":
                assert frame.body["name"] in COMMAND_NAMES


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/ping device", {"type": "ping"}),
        ("/ping router", {"name": "net.ping", "args": {"target": "router"}}),
        ("/ping llm", {"name": "net.ping", "args": {"target": "llm"}}),
        ("/ping ntp", {"name": "net.ping", "args": {"target": "ntp"}}),
    ],
)
def test_ping_targets_map_to_correct_frames(text, expected):
    """`05-bridge.md` §7: `device` is the latency check, the rest are reachability."""
    frame = parse(text)[0]
    assert frame.type == expected.get("type", "command")
    if "name" in expected:
        assert frame.body["name"] == expected["name"]
        assert frame.body["args"] == expected["args"]


def test_bare_and_bogus_ping_raise_the_usage_error():
    usage = "usage: /ping device|router|llm|ntp"
    for text in ("/ping", "/ping foo", "/ping router extra"):
        with pytest.raises(CommandError, match=usage.replace("|", "\\|")):
            parse(text, CommandState())


def test_a_ping_target_never_falls_back_to_device():
    """The user's request: `/ping` alone is an error, not an alias for `/ping device`."""
    with pytest.raises(CommandError):
        parse("/ping", CommandState())



@pytest.mark.parametrize(
    "path",
    [
        "/config.json",
        "/tasks.json",
        "/skills/brief.md",
        "/prompts/daily.md",
        "/tools/weather_now.json",
    ],
)
def test_fs_put_accepts_every_path_the_device_allowlists(path):
    """`04` §4.16 lists the two documents and the three directories an import may
    target. A path the device accepts must not be refused here, or the command is
    dead for a file the device would have taken."""
    (frame,) = parse(f"/fs put {path}", CommandState(upload_bytes=b"a"))
    assert frame.body["args"]["path"] == path


@pytest.mark.parametrize(
    "path",
    [
        "/workspace/a.txt",  # the sandbox is not userfs
        "/skills/a.txt",  # the directory fixes the extension
        "/tools/a.md",
        "/prompts/.md",  # a dotfile has no stem and would list as a template
        "/skills/",
        "/config.json.bak",
        "/skills/../workspace/a.md",  # canonicalises out of the allowlist
        "/",
    ],
)
def test_fs_put_refuses_a_path_outside_the_allowlist_before_sending(path):
    """`05` §7: the bridge refuses before sending anything, so a doomed upload
    costs no round trip. The message names the allowlist rather than repeating the
    path, because the reader has to know what would have worked."""
    with pytest.raises(CommandError) as exc:
        parse(f"/fs put {path}", CommandState(upload_bytes=b"a"))
    assert str(exc.value) == (
        "path is outside /config.json, /tasks.json, /skills/, /prompts/, /tools/"
    )


def test_the_ui_only_commands_still_parse_to_one_frame():
    """`05` §7.6 and §7.7: `/wifi list`, `/wifi edit` and `/providers` are answered by
    the screen, but `--send` goes through `parse`, so each must name a frame the
    device understands rather than raising. `/wifi list` reads the station, and
    `/wifi edit` and `/providers` open with the document."""
    assert parse("/wifi list")[0].body["name"] == "wifi.status"
    assert parse("/wifi edit")[0].body["name"] == "config.get"
    assert parse("/providers")[0].body["name"] == "config.get"


def test_secret_delete_is_its_own_frame_and_reads_no_value():
    """`04` §4.31: erasure is a command, never a blank `secret.set`, so it needs no
    value from the prompt — a state with none still parses."""
    (frame,) = parse("/secret delete wifi_psk", CommandState())
    assert frame.body["name"] == "secret.delete"
    assert frame.body["args"] == {"key": "wifi_psk"}


def test_tools_bare_still_asks_for_the_catalogue():
    """`05` §7.4: the UI intercepts `/tools`, but the `--send` path must still produce
    the list frame rather than fall through."""
    (frame,) = parse("/tools")
    assert frame.body["name"] == "tools.list"
