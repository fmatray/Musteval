/* Shared test helpers.
 *
 * `catf` exists because `at += snprintf(buf + at, cap - at, ...)` is a latent
 * overflow: `snprintf` returns the length it *would* have written, so once the
 * buffer fills, `at` exceeds `cap`, `cap - at` underflows to a huge `size_t`, and
 * the next call writes past the end. `catf` clamps instead, so a fixture that
 * outgrows its buffer fails its own assertion rather than corrupting memory.
 *
 * It is a header-local `static inline` so the three suites that build JSON by hand
 * share one implementation without a new translation unit in `SRCS`.
 */
#ifndef TEST_UTIL_H
#define TEST_UTIL_H

#include <stdarg.h>
#include <stddef.h>
#include <stdio.h>

#include "unity.h"

/* Append `fmt` at `at`. Returns the new offset, never past `cap - 1`, and fails the
 * test rather than truncating silently: a fixture that cannot hold its own input is
 * a fixture whose assertion would pass for the wrong reason. */
static inline size_t catf(char *buf, size_t cap, size_t at, const char *fmt, ...)
{
    TEST_ASSERT_TRUE_MESSAGE(at < cap, "catf: the fixture buffer is already full");
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf + at, cap - at, fmt, ap);
    va_end(ap);
    TEST_ASSERT_TRUE_MESSAGE(n >= 0, "catf: formatting failed");
    TEST_ASSERT_TRUE_MESSAGE((size_t)n < cap - at, "catf: the fixture buffer is too small");
    return at + (size_t)n;
}

#endif
