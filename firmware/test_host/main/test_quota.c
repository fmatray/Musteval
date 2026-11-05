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
