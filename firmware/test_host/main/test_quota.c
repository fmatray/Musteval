/* Workspace quota and chunk sequencing: `06-security.md` §6.2 and
 * `04-protocol.md` §4.16, §4.18. Both are pure arithmetic, so they are provable
 * without a filesystem. */

#include <string.h>

#include "unity.h"

#include "store.h"

static store_quota_t quota(size_t files, size_t bytes)
{
    store_quota_t q = {.files = files, .bytes = bytes};
    return q;
}

TEST_CASE("a new file beyond the total quota is refused", "[quota]")
{
    store_quota_t q = quota(0, STORE_WS_TOTAL_BYTES - 10);
    TEST_ASSERT_FALSE(store_quota_exceeded(&q, 10)); /* exactly full is allowed */
    TEST_ASSERT_TRUE(store_quota_exceeded(&q, 11));
}

TEST_CASE("overwriting reclaims the old size instead of adding to it", "[quota]")
{
    /* 06 §6.2: an overwrite counts the new size, never both. A tree that is full
     * because this same file is large can still rewrite it at the same size. */
    store_quota_t q = quota(1, STORE_WS_TOTAL_BYTES);
    q.replacing = true;
    q.replacing_bytes = STORE_WS_FILE_BYTES;

    TEST_ASSERT_FALSE(store_quota_exceeded(&q, STORE_WS_FILE_BYTES));
    /* Growing past the per-file cap is refused even though the total would fit. */
    TEST_ASSERT_TRUE(store_quota_exceeded(&q, STORE_WS_FILE_BYTES + 1));
}

TEST_CASE("the entry cap counts only files a write would add", "[quota]")
{
    store_quota_t full = quota(STORE_WS_FILE_COUNT, 0);
    TEST_ASSERT_TRUE(store_quota_exceeded(&full, 1));

    /* An overwrite adds no entry, so it is not refused for the count. */
    store_quota_t full_replace = quota(STORE_WS_FILE_COUNT, 0);
    full_replace.replacing = true;
    TEST_ASSERT_FALSE(store_quota_exceeded(&full_replace, 1));
}

TEST_CASE("an already-over-quota tree refuses every write", "[quota]")
{
    /* Reachable when a previous limit was raised or accounting drifted; refusing
     * is the safe direction because the check must never under-report. */
    store_quota_t q = quota(1, STORE_WS_TOTAL_BYTES + 1);
    TEST_ASSERT_TRUE(store_quota_exceeded(&q, 0));
}

TEST_CASE("an inconsistent reading does not invent free space", "[quota]")
{
    /* replacing_bytes larger than the whole tree is impossible; if it happens the
     * reclaim must not wrap the subtraction into a huge budget. */
    store_quota_t q = quota(1, 100);
    q.replacing = true;
    q.replacing_bytes = 900;
    TEST_ASSERT_TRUE(store_quota_exceeded(&q, STORE_WS_TOTAL_BYTES));
}

TEST_CASE("the import cap is the target directory's per-file limit", "[quota]")
{
    /* 03-config.md §Storage limits: imports get no larger budget than the file
     * they could have replaced. */
    TEST_ASSERT_EQUAL_size_t(8u * 1024u, store_import_cap("/skills/led.md"));
    TEST_ASSERT_EQUAL_size_t(4u * 1024u, store_import_cap("/prompts/brief.md"));
    TEST_ASSERT_EQUAL_size_t(2u * 1024u, store_import_cap("/tools/gpio.json"));
    TEST_ASSERT_EQUAL_size_t(8u * 1024u, store_import_cap("/config.json"));
    TEST_ASSERT_EQUAL_size_t(8u * 1024u, store_import_cap("/tasks.json"));
    /* The sandbox has no per-file cap; the workspace quota bounds it instead. */
    TEST_ASSERT_EQUAL_size_t(0, store_import_cap("/workspace/notes.md"));
    TEST_ASSERT_EQUAL_size_t(0, store_import_cap(NULL));
}

/* 04 §4.16: an import lands on exactly one of five targets, and the directory
 * fixes the extension, because each one carries exactly one file type. A name
 * without the suffix would be listed as a template or tool and never parsed. */
TEST_CASE("an import path is one of the five legal targets", "[quota]")
{
    TEST_ASSERT_TRUE(store_import_path_ok("/config.json"));
    TEST_ASSERT_TRUE(store_import_path_ok("/tasks.json"));
    TEST_ASSERT_TRUE(store_import_path_ok("/skills/led-control.md"));
    TEST_ASSERT_TRUE(store_import_path_ok("/prompts/brief.md"));
    TEST_ASSERT_TRUE(store_import_path_ok("/tools/gpio.json"));

    TEST_ASSERT_FALSE(store_import_path_ok(NULL));
    TEST_ASSERT_FALSE(store_import_path_ok(""));
    TEST_ASSERT_FALSE(store_import_path_ok("/workspace/notes.md"));
    TEST_ASSERT_FALSE(store_import_path_ok("/config.json.bak"));

    /* The extension is the directory's, not any: a `.md` tool never parses. */
    TEST_ASSERT_FALSE(store_import_path_ok("/skills/led.txt"));
    TEST_ASSERT_FALSE(store_import_path_ok("/tools/gpio.md"));
    TEST_ASSERT_FALSE(store_import_path_ok("/prompts/brief.json"));

    /* A bare extension is a dotfile, not a name, and would list as something no
     * one can ask for. */
    TEST_ASSERT_FALSE(store_import_path_ok("/prompts/.md"));
    TEST_ASSERT_FALSE(store_import_path_ok("/skills/"));

    /* A nested path is accepted: 04 §4.16 says "under /skills/", which is a
     * prefix, and the listing simply does not descend. */
    TEST_ASSERT_TRUE(store_import_path_ok("/skills/sub/led.md"));

    /* The rule is on the canonical path, because that is the path written. A raw
     * prefix match would admit `/skills/../workspace/x.md`: it canonicalises into
     * the sandbox, where the quota bounds it, yet `store_import_cap` would read the
     * raw `/skills/` prefix and grant the larger image cap — a quota bypass. */
    TEST_ASSERT_FALSE(store_import_path_ok("/skills/../workspace/x.md"));
    TEST_ASSERT_EQUAL_size_t(0, store_import_cap("/skills/../workspace/x.md"));

    /* The same rule must not over-refuse: a path that canonicalises back into a
     * legal directory is still that directory's file. */
    TEST_ASSERT_TRUE(store_import_path_ok("/prompts/./../prompts/x.md"));
    TEST_ASSERT_EQUAL_size_t(4u * 1024u, store_import_cap("/prompts/./../prompts/x.md"));
}

TEST_CASE("a chunk must continue where the last one stopped", "[quota]")
{
    /* 04 §4.16: two interleaved transfers fail the offset check rather than
     * splicing their bytes into one file. */
    TEST_ASSERT_TRUE(store_chunk_fits(0, 10, 0));    /* starts a transfer */
    TEST_ASSERT_TRUE(store_chunk_fits(0, 10, 999));  /* offset 0 always restarts */
    TEST_ASSERT_TRUE(store_chunk_fits(10, 10, 10));  /* contiguous */
    TEST_ASSERT_FALSE(store_chunk_fits(11, 10, 10)); /* gap */
    TEST_ASSERT_FALSE(store_chunk_fits(5, 10, 10));  /* overlap */
}

TEST_CASE("a chunk over 2048 raw bytes is refused wherever it lands", "[quota]")
{
    TEST_ASSERT_TRUE(store_chunk_fits(0, STORE_CHUNK_BYTES, 0));
    TEST_ASSERT_FALSE(store_chunk_fits(0, STORE_CHUNK_BYTES + 1, 0));
    TEST_ASSERT_FALSE(store_chunk_fits(0, STORE_CHUNK_BYTES + 1, 4096));
}
