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
