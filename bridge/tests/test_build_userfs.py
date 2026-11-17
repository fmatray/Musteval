"""`bridge/tools/build_userfs.py` — the userfs image builder (`05-bridge.md` §11)."""

import os
import sys
import re
import subprocess
from pathlib import Path
import pytest

# Ensure the tool is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge.tools.build_userfs as builder

def test_the_partition_offset_and_size_come_from_the_real_table():
    # Real table check
    offset, size = builder.partition("userfs")
    assert offset == 0x320000
    assert size == 0x3C0000
    
    # No hardcoding: verify by using a different table
    tmp_csv = Path("temp_partitions.csv")
    tmp_csv.write_text("name,type,subtype,offset,size,flags\nuserfs,data,littlefs,0x1000,0x2000,")
    try:
        offset, size = builder.partition("userfs", path=tmp_csv)
        assert offset == 0x1000
        assert size == 0x2000
    finally:
        tmp_csv.unlink()

def test_a_missing_partition_is_a_loud_error(tmp_path):
    csv = tmp_path / "partitions.csv"
    csv.write_text("name,type,subtype,offset,size,flags\nwsfs,data,littlefs,0x1000,0x2000,")
    with pytest.raises(builder.ToolError, match="has no partition named 'userfs'"):
        builder.partition("userfs", path=csv)

def test_the_object_name_length_comes_from_sdkconfig():
    # Real sdkconfig check (should be 64 if built)
    try:
        val = builder.obj_name_len()
        assert val == 64
    except builder.ToolError:
        pytest.skip("sdkconfig missing; cannot verify real value")
    
    # No hardcoding: verify by using a different file
    tmp_sdk = Path("temp_sdkconfig")
    tmp_sdk.write_text("CONFIG_LITTLEFS_OBJ_NAME_LEN=48\n")
    try:
        assert builder.obj_name_len(path=tmp_sdk) == 48
    finally:
        tmp_sdk.unlink()

def test_no_interpreter_with_littlefs_names_the_install_command():
    # Mock candidates to be empty
    original_candidates = builder._candidates
    builder._candidates = lambda x: []
    try:
        with pytest.raises(builder.ToolError) as exc:
            builder.find_python()
        assert "pip install littlefs-python==0.15.0" in str(exc.value)
    finally:
        builder._candidates = original_candidates

def test_the_image_round_trips_through_a_built_image(tmp_path):
    """
    Asserts that build_image produces a valid image that can be opened
    and listed by the littlefs module.
    """
    # 1. Find an interpreter that can actually run littlefs
    try:
        py = builder.find_python()
    except builder.ToolError as exc:
        pytest.skip(f"littlefs-python is not installed in any interpreter: {exc}")
    
    # 2. Setup a fixture source tree
    src = tmp_path / "src"
    src.mkdir()
    (src / "file1.txt").write_text("content1")
    (src / "dir1").mkdir()
    (src / "dir1" / "file2.txt").write_text("content2")
    
    out = tmp_path / "userfs.bin"
    size = 0x3C0000
    name_max = 64
    
    # 3. Build the image
    builder.build_image(src, out, size, name_max, python=py)
    assert out.stat().st_size == size
    
    # 4. Reopen and list with the module (via subprocess for isolation)
    # We use 'littlefs list' which the script already uses for its feedback
    res = subprocess.run(
        [py, "-m", "littlefs", "list", str(out), 
         f"--block-size={builder.BLOCK_SIZE}", f"--name-max={name_max}"],
        capture_output=True, text=True, check=True
    )
    
    assert "/file1.txt" in res.stdout
    assert "/dir1" in res.stdout
    assert "/dir1/file2.txt" in res.stdout

def test_the_seed_config_is_inside_the_documented_bounds():
    """
    Parses the seed config and verifies:
    - It is a valid JSON.
    - It contains required top-level keys (version, wifi, llm).
    - required arrays are non-empty.
    - all nvs refs match the regex ^nvs:[A-Za-z0-9_]{1,15}$.
    - a host allowlist for shipped tools is present.
    """
    import json
    seed = Path("firmware/userfs/config.json")
    assert seed.is_file()
    cfg = json.loads(seed.read_text(encoding="utf-8"))
    
    assert cfg["version"] == 1
    
    # Wifi networks required 1..8
    nets = cfg.get("wifi", {}).get("networks", [])
    assert 1 <= len(nets) <= 8
    for net in nets:
        assert 1 <= len(net["ssid"]) <= 32
        assert re.match(r"^nvs:[A-Za-z0-9_]{1,15}$", net["password_ref"])
        
    # LLM providers required 1..4
    provs = cfg.get("llm", {}).get("providers", [])
    assert 1 <= len(provs) <= 4
    for p in provs:
        assert "id" in p and "base_url" in p and "model" in p and "api_key_ref" in p
        assert re.match(r"^nvs:[A-Za-z0-9_]{1,15}$", p["api_key_ref"])
        
    # Tools allowlist for shipped tools
    hosts = cfg.get("tools", {}).get("http_allow_hosts", [])
    assert "api.open-meteo.com" in hosts
    assert "api.search.brave.com" in hosts
    assert len(hosts) <= 8

def test_the_seed_tool_files_are_the_documented_fences():
    """
    Extracts the JSON fences from docs/03-config.md and compares 
    them byte-for-byte to the seed tool files.
    """
    import re
    doc = (Path("docs/03-config.md")).read_text(encoding="utf-8")
    section = re.search(r"### Shipped tools(.*?)(?=###)", doc, re.S | re.I)
    assert section is not None
    fences = re.findall(r"```json\n(.*?)\n```", section.group(1), re.S)
    assert len(fences) >= 2
    
    tool_files = ["tools/weather_now.json", "tools/web_search.json"]
    for i, rel_path in enumerate(tool_files):
        file_path = Path("firmware/userfs") / rel_path
        assert file_path.is_file()
        # Compare exactly (fences were written verbatim to files)
        assert file_path.read_text(encoding="utf-8") == fences[i]

def test_the_seed_system_prompt_requires_a_todo_list():
    prompt = (Path("firmware/userfs/prompts/system.md")).read_text(encoding="utf-8")
    assert "todo" in prompt.lower()
    assert "init" in prompt.lower()
    assert "done" in prompt.lower()
    assert "drop" in prompt.lower()


def test_the_seed_system_prompt_is_short_enough_to_carry_in_every_request():
    """`03-config.md` §prompts: `system.md` fits in at most 50 lines of at most 80
    columns. The persona is prepended to every request and shares
    `agent.max_input_bytes` with the conversation, so its length is a budget rather
    than a taste: a prompt over it crowds out the history it exists to frame."""
    lines = Path("firmware/userfs/prompts/system.md").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 50
    over = [(number, len(line)) for number, line in enumerate(lines, 1) if len(line) > 80]
    assert over == []


def test_the_seed_system_prompt_only_recommends_mermaid_kinds_that_render():
    """`05` §5.1: `bridge/mermaid.rewrite` draws `flowchart`, `graph` and
    `stateDiagram-v2` and returns the fence untouched for anything else. A prompt
    that names a kind the pane will not draw produces a directive the device cannot
    follow, so every kind it mentions is fed to the renderer.

    The probe is the header as the prompt writes it, direction included: `_HEAD`
    requires one, so a prompt naming a bare `flowchart` would send the model to a
    fence the pane leaves as code even though the kind is supported."""
    import re

    from bridge.mermaid import rewrite

    prompt = Path("firmware/userfs/prompts/system.md").read_text(encoding="utf-8")
    headers = re.findall(
        r"\b(flowchart(?: (?:TD|TB|BT|LR|RL))?|graph(?: (?:TD|TB|BT|LR|RL))?"
        r"|stateDiagram(?:-v2)?|sequenceDiagram|classDiagram|erDiagram|journey"
        r"|gantt|pie|mindmap)\b",
        prompt,
    )
    assert headers, "the prompt names no mermaid kind"
    for header in set(headers):
        fence = f"```mermaid\n{header}\n  A --> B: go\n```"
        assert rewrite(fence) != fence, (
            f"the prompt names {header!r}, which the chat pane leaves as code"
        )


def test_the_seed_skill_satisfies_the_frontmatter_schema():
    import re
    skill_file = Path("firmware/userfs/skills/led-control.md")
    assert skill_file.is_file()
    text = skill_file.read_text(encoding="utf-8")
    
    # Frontmatter check
    match = re.search(r"^---\n(.*?)\n---", text, re.S | re.M)
    assert match is not None
    fm = match.group(1)
    
    # Name == Stem
    assert "name: led-control" in fm
    # Description’s length
    desc_match = re.search(r"description: (.*)", fm)
    assert desc_match and len(desc_match.group(1)) <= 200
    # version, keywords, tools
    assert "version: 1.0.0" in fm
    assert "tools: [gpio.read, gpio.write]" in fm
    assert "max_body_bytes: 2048" in fm
    
    # Body size
    body = text[match.end():]
    assert len(body.encode("utf-8")) <= 4096

def test_the_seed_ships_no_tasks_json():
    assert not (Path("firmware/userfs/tasks.json")).exists()

def _littlefs_python() -> str:
    """Finds a littlefs-compatible interpreter or skips the test."""
    try:
        return builder.find_python()
    except builder.ToolError as exc:
        pytest.skip(f"littlefs-python not installed: {exc}")

def test_a_missing_partition_table_is_a_loud_error(tmp_path):
    with pytest.raises(builder.ToolError, match="partition table not found"):
        builder.partition("userfs", path=tmp_path / "partitions.csv")

def test_a_missing_sdkconfig_asks_for_a_firmware_build(tmp_path):
    with pytest.raises(builder.ToolError, match="build the firmware once first"):
        builder.obj_name_len(path=tmp_path / "sdkconfig")

def test_an_sdkconfig_without_the_object_name_length_is_refused(tmp_path):
    sdk = tmp_path / "sdkconfig"
    sdk.write_text("CONFIG_LITTLEFS_SOMETING=1\n")
    with pytest.raises(builder.ToolError, match="does not set CONFIG_LITTLEFS_OBJ_NAME_LEN"):
        builder.obj_name_len(path=sdk)

def test_the_probe_treats_a_non_executable_path_as_no_littlefs(tmp_path):
    # A path that is gone or not executable is a "no" from the probe.
    assert builder._imports_littlefs(str(tmp_path / "absent")) is False

def test_an_explicit_interpreter_is_the_only_one_tried():
    # --python is an answer or an error; no fallbacks are used.
    assert list(builder._candidates("/opt/esp/python")) == ["/opt/esp/python"]

def test_find_python_refuses_an_explicit_interpreter_without_littlefs(tmp_path):
    # Assert the refusal names the explicit path and suggests the install command.
    # we use sys.executable as it's guaranteed to exist but lacks littlefs in .venv.
    py = sys.executable
    with pytest.raises(builder.ToolError) as exc:
        builder.find_python(explicit=py)
    assert f"Tried: {py}" in str(exc.value)
    assert "pip install littlefs-python==0.15.0" in str(exc.value)

def test_esptool_prefers_the_one_beside_the_interpreter(tmp_path):
    py = tmp_path / "python"
    py.write_text("")
    sibling = tmp_path / "esptool.py"
    sibling.write_text("")
    assert builder.esptool(str(py)) == str(sibling)

def test_esptool_falls_back_to_path_and_refuses_when_absent(tmp_path, monkeypatch):
    # Case 1: sibling absent, but esptool is on PATH.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    found = bin_dir / "esptool"
    found.write_text("")
    found.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert builder.esptool(str(tmp_path / "python")) == str(found)
    # Case 2: neither available.
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(builder.ToolError, match="esptool.py not found"):
        builder.esptool(str(tmp_path / "python"))

def test_list_image_reports_what_the_filesystem_holds(tmp_path):
    py = _littlefs_python()
    src = tmp_path / "src"; src.mkdir()
    (src / "a.txt").write_text("a")
    (src / "sub").mkdir(); (src / "sub" / "b.md").write_text("b")
    image = builder.build_image(src, tmp_path / "img.bin", 0x4000, 64, python=py)
    assert sorted(builder.list_image(py, image)) == ["/a.txt", "/sub", "/sub/b.md"]

def test_build_image_refuses_a_source_that_is_not_a_directory(tmp_path):
    out = tmp_path / "img.bin"
    with pytest.raises(builder.ToolError, match="source directory not found"):
        builder.build_image(tmp_path / "gone", out, 0x2000, 64)
    assert not out.exists()

def test_build_image_refuses_an_image_that_is_not_the_partition_size(tmp_path, monkeypatch):
    # Guards against a misbehaving tool that produces a short image.
    def fake_run(*args, **kwargs):
        # The tool uses subprocess.run for both the littlefs probe and the build.
        cmd = args[0] if isinstance(args[0], list) else args
        if len(cmd) > 5 and "littlefs" in cmd and "create" in cmd:
            Path(cmd[5]).write_bytes(b"\0" * 10)
        return type('obj', (object,), {'returncode': 0, 'stdout': b'', 'stderr': b''})

    monkeypatch.setattr("subprocess.run", fake_run)
    src = tmp_path / "src"; src.mkdir()
    with pytest.raises(builder.ToolError) as exc:
        builder.build_image(src, tmp_path / "img.bin", 0x2000, 64)
    assert "8192" in str(exc.value)

def test_flash_uses_the_partition_offset_and_fails_on_a_dead_port(tmp_path, capfd):
    py = _littlefs_python()
    try:
        builder.esptool(py)
    except builder.ToolError:
        pytest.skip("esptool not available beside the interpreter")
    image = tmp_path / "img.bin"
    image.write_bytes(b"\0" * 0x2000)
    with pytest.raises(subprocess.CalledProcessError):
        builder.flash(image, "/dev/does-not-exist", 460800, python=py)
    err = capfd.readouterr().err
    assert "does-not-exist" in err

def test_main_builds_and_lists_without_flashing(tmp_path, capfd):
    py = _littlefs_python()
    src = tmp_path / "src"; src.mkdir()
    (src / "a.txt").write_text("hello")
    out = tmp_path / "build" / "userfs.bin"
    out.parent.mkdir(parents=True, exist_ok=True)
    code = builder.main(["--source", str(src), "-o", str(out), "--python", py])
    assert code == 0
    _, size = builder.partition("userfs")
    assert out.stat().st_size == size
    out_text = capfd.readouterr().out
    assert "/a.txt" in out_text
    assert "no --port: image built, flash not written" in out_text

def test_main_returns_two_when_no_interpreter_can_import_littlefs(tmp_path, capfd):
    code = builder.main(["--python", str(tmp_path / "gone")])
    assert code == 2
    assert "pip install littlefs-python" in capfd.readouterr().err

def test_main_reports_a_build_failure_and_returns_one(tmp_path, capfd):
    code = builder.main(["--source", str(tmp_path / "gone"), "-o", str(tmp_path / "i.bin")])
    assert code == 1
    assert "image build failed: source directory not found" in capfd.readouterr().err

def test_main_refuses_contradictory_flags():
    with pytest.raises(SystemExit) as exc:
        builder.main(["--build-only", "--port", "/dev/ttyUSB0"])
    assert exc.value.code == 2

def test_main_reports_a_flash_failure_as_exit_one(tmp_path, capfd):
    py = _littlefs_python()
    src = tmp_path / "src"; src.mkdir(); (src / "a.txt").write_text("a")
    code = builder.main(["--source", str(src), "-o", str(tmp_path / "i.bin"),
                         "--port", "/dev/does-not-exist", "--python", py])
    assert code == 1
    assert "flash failed" in capfd.readouterr().err

def test_the_script_entry_point_builds_and_exits_zero(tmp_path, monkeypatch):
    import runpy, sys
    src = tmp_path / "src"; src.mkdir(); (src / "a.txt").write_text("a")
    out = tmp_path / "i.bin"
    monkeypatch.setattr(sys, "argv", ["build_userfs.py", "--build-only", "--source", str(src), "-o", str(out)])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(Path("bridge/tools/build_userfs.py")), run_name="__main__")
    assert exc.value.code == 0
    assert out.stat().st_size == builder.partition("userfs")[1]

