/* Sandbox pure logic: `04-protocol.md` §4.17, §4.20. Entry ordering, name
 * acceptance and chunk decoding are decisions made before any filesystem call, so
 * they are provable on the host. The filesystem half of `store.c` is board-bound
 * and is covered by the on-target suite, not here. */

#include <stdio.h>
#include <string.h>

#include "unity.h"

#include "store.h"

static store_entry_t entry(const char *name, bool dir, size_t bytes)
{
    store_entry_t e = {.dir = dir, .bytes = bytes};
    snprintf(e.name, sizeof e.name, "%s", name);
    return e;
}

TEST_CASE("directories sort before files, then by name", "[sandbox]")
{
    /* 04 §4.17: the listing is not whatever readdir returns. Two entries that
     * compare equal in one respect must still be ordered by the other. */
    store_entry_t dir_b = entry("beta", true, 0);
    store_entry_t dir_a = entry("alpha", true, 0);
    store_entry_t file_z = entry("zeta", false, 10);
    store_entry_t file_a = entry("aaa", false, 5);

    TEST_ASSERT_LESS_THAN(0, store_entry_cmp(&dir_a, &dir_b));
    TEST_ASSERT_GREATER_THAN(0, store_entry_cmp(&dir_b, &dir_a));
    TEST_ASSERT_LESS_THAN(0, store_entry_cmp(&file_a, &file_z));
    /* A directory wins on kind even when its name sorts after the file's. */
    TEST_ASSERT_LESS_THAN(0, store_entry_cmp(&dir_b, &file_a));
    TEST_ASSERT_EQUAL(0, store_entry_cmp(&dir_a, &dir_a));
}

TEST_CASE("an entry name is one path segment", "[sandbox]")
{
    /* 04 §4.17: a name carrying a separator would render as a child path and let
     * the listing describe a tree it is not. */
    TEST_ASSERT_TRUE(store_entry_name_ok("notes.md"));
    TEST_ASSERT_TRUE(store_entry_name_ok("a b.txt"));
    TEST_ASSERT_FALSE(store_entry_name_ok(NULL));
    TEST_ASSERT_FALSE(store_entry_name_ok(""));
    TEST_ASSERT_FALSE(store_entry_name_ok("."));
    TEST_ASSERT_FALSE(store_entry_name_ok(".."));
    TEST_ASSERT_FALSE(store_entry_name_ok("sub/file"));
    TEST_ASSERT_FALSE(store_entry_name_ok("../escape"));
}

/* 04 §4.12: a template name is a bare filename of `[A-Za-z0-9._-]+\.md`. The rule is
 * the whole of `template.get`'s confinement — the name is concatenated onto
 * `/prompts/` after it passes — so a hole here reaches outside the directory. */
TEST_CASE("a template name is a bare .md filename", "[sandbox]")
{
    TEST_ASSERT_TRUE(store_template_name_ok("brief.md"));
    TEST_ASSERT_TRUE(store_template_name_ok("boot_summary.md"));
    TEST_ASSERT_TRUE(store_template_name_ok("a-b_c.1.md"));
    TEST_ASSERT_TRUE(store_template_name_ok("..md2.md"));

    TEST_ASSERT_FALSE(store_template_name_ok(NULL));
    TEST_ASSERT_FALSE(store_template_name_ok(""));
    TEST_ASSERT_FALSE(store_template_name_ok(".md"));
    TEST_ASSERT_FALSE(store_template_name_ok("..md"));
    TEST_ASSERT_FALSE(store_template_name_ok("brief"));
    TEST_ASSERT_FALSE(store_template_name_ok("brief.txt"));
    TEST_ASSERT_FALSE(store_template_name_ok("../brief.md"));
    TEST_ASSERT_FALSE(store_template_name_ok("sub/brief.md"));
    TEST_ASSERT_FALSE(store_template_name_ok("brief md"));
    TEST_ASSERT_FALSE(store_template_name_ok("brief\\md"));
}

TEST_CASE("a utf8 chunk is copied verbatim", "[sandbox]")
{
    uint8_t out[STORE_CHUNK_BYTES];
    size_t len = 0;
    const char *data = "hello";

    /* A NULL encoding is the default, not an error. */
    TEST_ASSERT_EQUAL(ESP_OK, store_decode_chunk(data, NULL, out, sizeof out, &len));
    TEST_ASSERT_EQUAL_size_t(5, len);
    TEST_ASSERT_EQUAL_MEMORY("hello", out, 5);

    TEST_ASSERT_EQUAL(ESP_OK, store_decode_chunk(data, "utf8", out, sizeof out, &len));
    TEST_ASSERT_EQUAL_size_t(5, len);
    TEST_ASSERT_EQUAL_MEMORY("hello", out, 5);
}

TEST_CASE("a base64 chunk decodes to its bytes", "[sandbox]")
{
    uint8_t out[STORE_CHUNK_BYTES];
    size_t len = 0;

    /* "aGVsbG8=" is the base64 of "hello". */
    TEST_ASSERT_EQUAL(ESP_OK, store_decode_chunk("aGVsbG8=", "base64", out, sizeof out, &len));
    TEST_ASSERT_EQUAL_size_t(5, len);
    TEST_ASSERT_EQUAL_MEMORY("hello", out, 5);
}

TEST_CASE("a malformed or oversized chunk is refused, not truncated", "[sandbox]")
{
    /* 04 §4.16, §4.20: a short write that reported success would corrupt the file
     * silently, so every refusal is an error and `*len` stays 0. */
    uint8_t out[STORE_CHUNK_BYTES];
    size_t len = 99;

    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, store_decode_chunk("not base64!", "base64", out, sizeof out, &len));
    TEST_ASSERT_EQUAL_size_t(0, len);
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, store_decode_chunk("aGVsbG8=", "rot13", out, sizeof out, &len));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, store_decode_chunk(NULL, "utf8", out, sizeof out, &len));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, store_decode_chunk("x", "utf8", NULL, 1, &len));
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, store_decode_chunk("x", "utf8", out, sizeof out, NULL));
}

TEST_CASE("a chunk that does not fit the destination is refused", "[sandbox]")
{
    /* The caller's buffer is the raw cap. A chunk one byte larger must fail rather
     * than fill it and lose the tail. */
    uint8_t small[4];
    size_t len = 0;
    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG, store_decode_chunk("hello", "utf8", small, sizeof small, &len));
    TEST_ASSERT_EQUAL(ESP_OK, store_decode_chunk("hell", "utf8", small, sizeof small, &len));
    TEST_ASSERT_EQUAL_size_t(4, len);
}

TEST_CASE("a chunk over the protocol limit is refused whatever the buffer", "[sandbox]")
{
    /* 04 §4.16 caps one chunk at STORE_CHUNK_BYTES regardless of how much room the
     * caller offers, so a peer cannot assemble a bigger frame by asking nicely. */
    static uint8_t big[STORE_CHUNK_BYTES + 1];
    static char text[STORE_CHUNK_BYTES + 1];
    memset(text, 'a', STORE_CHUNK_BYTES + 1);
    size_t len = 0;

    TEST_ASSERT_EQUAL(ESP_ERR_INVALID_ARG,
                      store_decode_chunk(text, "utf8", big, sizeof big, &len));

    /* One byte shorter is the boundary and must pass, so the test proves the cap
     * is a limit rather than a blanket refusal. */
    text[STORE_CHUNK_BYTES] = '\0';
    TEST_ASSERT_EQUAL(ESP_OK, store_decode_chunk(text, "utf8", big, sizeof big, &len));
    TEST_ASSERT_EQUAL_size_t(STORE_CHUNK_BYTES, len);
}

TEST_CASE("valid utf8 is accepted whole", "[sandbox]")
{
    const uint8_t ascii[] = "plain";
    const uint8_t two[] = {0xC3, 0xA9};                    /* é */
    const uint8_t three[] = {0xE2, 0x82, 0xAC};            /* € */
    const uint8_t four[] = {0xF0, 0x9F, 0xA6, 0xA1};       /* 🦡 */
    TEST_ASSERT_EQUAL_size_t(5, store_utf8_prefix(ascii, 5));
    TEST_ASSERT_EQUAL_size_t(2, store_utf8_prefix(two, 2));
    TEST_ASSERT_EQUAL_size_t(3, store_utf8_prefix(three, 3));
    TEST_ASSERT_EQUAL_size_t(4, store_utf8_prefix(four, 4));
    TEST_ASSERT_EQUAL_size_t(0, store_utf8_prefix(NULL, 3));
}

TEST_CASE("overlong and out-of-range utf8 is refused", "[sandbox]")
{
    /* 04 §4.18: invalid UTF-8 is E_STORAGE, never mojibake on screen. The old
     * validator accepted every one of these, which is why each is pinned here. */
    const uint8_t overlong2[] = {0xC0, 0xAF};              /* '/' as two bytes */
    const uint8_t overlong3[] = {0xE0, 0x80, 0xAF};
    const uint8_t overlong4[] = {0xF0, 0x80, 0x80, 0xAF};
    const uint8_t surrogate[] = {0xED, 0xA0, 0x80};        /* U+D800 */
    const uint8_t too_high[] = {0xF4, 0x90, 0x80, 0x80};   /* above U+10FFFF */
    const uint8_t lone_cont[] = {0x80};
    const uint8_t bad_cont[] = {0xC3, 0x28};

    TEST_ASSERT_EQUAL_size_t(0, store_utf8_prefix(overlong2, 2));
    TEST_ASSERT_EQUAL_size_t(0, store_utf8_prefix(overlong3, 3));
    TEST_ASSERT_EQUAL_size_t(0, store_utf8_prefix(overlong4, 4));
    TEST_ASSERT_EQUAL_size_t(0, store_utf8_prefix(surrogate, 3));
    TEST_ASSERT_EQUAL_size_t(0, store_utf8_prefix(too_high, 4));
    TEST_ASSERT_EQUAL_size_t(0, store_utf8_prefix(lone_cont, 1));
    TEST_ASSERT_EQUAL_size_t(0, store_utf8_prefix(bad_cont, 2));
}

TEST_CASE("a sequence cut by the read cap is a truncation, not an error", "[sandbox]")
{
    /* The read cap cuts text mid-sequence; that must shorten the text, not refuse
     * the file. Only bytes that cannot begin a valid sequence are an error. */
    const uint8_t cut[] = {'a', 'b', 0xE2, 0x82};          /* missing the last byte */
    TEST_ASSERT_EQUAL_size_t(2, store_utf8_prefix(cut, 4));
}

TEST_CASE("a file of exactly the cap is complete, not truncated", "[sandbox]")
{
    /* 04 §4.18: the cap is what one reply may carry. A full-cap read that consumed
     * the whole file is the whole file, and reporting it truncated would tell the
     * bridge to refuse a file it already holds in full. */
    TEST_ASSERT_FALSE(store_read_truncated(8192, 8192, 8192));
    TEST_ASSERT_TRUE(store_read_truncated(8192, 8192, 8193));
    TEST_ASSERT_FALSE(store_read_truncated(10, 8192, 10));
    /* A short read happens only at end of file, so it is never a truncation. */
    TEST_ASSERT_FALSE(store_read_truncated(0, 8192, 0));
}

TEST_CASE("an in-flight staging file is recognised and hidden", "[sandbox]")
{
    const char *dir = "/ws/";
    TEST_ASSERT_TRUE(store_is_staging_entry(dir, "a.txt.new", "/ws/a.txt.new"));
}

TEST_CASE("a staging predicate names nothing else", "[sandbox]")
{
    const char *dir = "/ws/";
    /* The bug this guards: appending `.new` to an entry name that already carries
     * it doubles the suffix and hides nothing, so the debris stayed visible. */
    TEST_ASSERT_FALSE(store_is_staging_entry(dir, "keep.txt", "/ws/a.txt.new"));
    TEST_ASSERT_FALSE(store_is_staging_entry(dir, "a.txt.new.new", "/ws/a.txt.new"));
    TEST_ASSERT_FALSE(store_is_staging_entry(dir, "a.txt", "/ws/a.txt.new"));
    /* An idle staging slot hides nothing at all. */
    TEST_ASSERT_FALSE(store_is_staging_entry(dir, "a.txt.new", ""));
    /* A directory without the separator still matches its own child. */
    TEST_ASSERT_TRUE(store_is_staging_entry("/ws", "a.txt.new", "/ws/a.txt.new"));
    TEST_ASSERT_FALSE(store_is_staging_entry("/ws2/", "a.txt.new", "/ws/a.txt.new"));
    TEST_ASSERT_FALSE(store_is_staging_entry(NULL, "a.txt.new", "/ws/a.txt.new"));
}

TEST_CASE("a populated directory needs recursive, an empty one does not", "[sandbox]")
{
    TEST_ASSERT_EQUAL(STORE_DELETE_FILE, store_delete_action(false, 0, false));
    TEST_ASSERT_EQUAL(STORE_DELETE_FILE, store_delete_action(false, 0, true));
    TEST_ASSERT_EQUAL(STORE_DELETE_EMPTY, store_delete_action(true, 0, false));
    TEST_ASSERT_EQUAL(STORE_DELETE_EMPTY, store_delete_action(true, 0, true));
    TEST_ASSERT_EQUAL(STORE_DELETE_NEEDS_RECURSIVE, store_delete_action(true, 3, false));
    TEST_ASSERT_EQUAL(STORE_DELETE_TREE, store_delete_action(true, 3, true));
}

TEST_CASE("only listable names count as directory children", "[sandbox]")
{
    size_t n = 0;
    n = store_count_entry(n, ".");
    n = store_count_entry(n, "..");
    TEST_ASSERT_EQUAL_size_t(0, n);
    n = store_count_entry(n, "notes.md");
    n = store_count_entry(n, "runs");
    TEST_ASSERT_EQUAL_size_t(2, n);
    TEST_ASSERT_EQUAL_size_t(2, store_count_entry(2, NULL));
    TEST_ASSERT_EQUAL_size_t(2, store_count_entry(2, "a/b"));
}

TEST_CASE("a move never overwrites its target", "[sandbox]")
{
    TEST_ASSERT_EQUAL(STORE_MOVE_TARGET_TAKEN,
                      store_move_verdict(true, true, "/workspace/a", "/workspace/b"));
    TEST_ASSERT_EQUAL(STORE_MOVE_OK,
                      store_move_verdict(true, false, "/workspace/a", "/workspace/b"));
    TEST_ASSERT_EQUAL(STORE_MOVE_SOURCE_MISSING,
                      store_move_verdict(false, false, "/workspace/gone", "/workspace/x"));
}

TEST_CASE("a move refuses the root and a directory into itself", "[sandbox]")
{
    TEST_ASSERT_EQUAL(STORE_MOVE_INTO_ITSELF,
                      store_move_verdict(true, false, "/workspace/runs", "/workspace/runs"));
    TEST_ASSERT_EQUAL(STORE_MOVE_INTO_ITSELF,
                      store_move_verdict(true, false, "/workspace/runs", "/workspace/runs/2026"));
    TEST_ASSERT_EQUAL(STORE_MOVE_INTO_ITSELF,
                      store_move_verdict(true, false, "/workspace", "/workspace/runs"));
    TEST_ASSERT_EQUAL(STORE_MOVE_OK,
                      store_move_verdict(true, false, "/workspace/run", "/workspace/runs"));
}
