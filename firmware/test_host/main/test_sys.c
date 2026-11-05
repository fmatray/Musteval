/* Boot-guard policy: pure arithmetic with no NVS, so it runs on the host
 * (`02-firmware.md` §3.2). The boundary is `07-roadmap.md` M0's acceptance. */

#include "unity.h"

#include "sys.h"

TEST_CASE("the counter increments and saturates", "[sys]")
{
    /* §3.1 step 1 increments before config is readable, so a fresh device boots
     * at 1, not 0. */
    TEST_ASSERT_EQUAL_INT32(1, boot_guard_next_count(0));
    TEST_ASSERT_EQUAL_INT32(4, boot_guard_next_count(3));
    /* A counter that cannot be read back must not reach the threshold by wrapping. */
    TEST_ASSERT_EQUAL_INT32(INT32_MAX, boot_guard_next_count(INT32_MAX));
    /* A corrupt read is a fresh device: NVS garbage alone must not trip the guard. */
    TEST_ASSERT_EQUAL_INT32(1, boot_guard_next_count(-5));
}

TEST_CASE("three failed boots put the fourth into safe mode", "[sys]")
{
    /* 07-roadmap.md M0: three consecutive boots that halt, then the fourth is
     * safe mode. The count includes the boot in progress, so this is `> 3`. */
    TEST_ASSERT_FALSE(boot_guard_should_enter_safe_mode(1, 3));
    TEST_ASSERT_FALSE(boot_guard_should_enter_safe_mode(3, 3));
    TEST_ASSERT_TRUE(boot_guard_should_enter_safe_mode(4, 3));
}

TEST_CASE("a threshold outside 2..10 falls back to the default", "[sys]")
{
    TEST_ASSERT_EQUAL_INT32(SYS_SAFE_MODE_AFTER_DEFAULT,
                            boot_guard_clamp_threshold(0));
    TEST_ASSERT_EQUAL_INT32(SYS_SAFE_MODE_AFTER_DEFAULT,
                            boot_guard_clamp_threshold(11));
    TEST_ASSERT_EQUAL_INT32(SYS_SAFE_MODE_AFTER_DEFAULT,
                            boot_guard_clamp_threshold(-1));
    /* Inside the range the configured value wins, including both bounds. */
    TEST_ASSERT_EQUAL_INT32(2, boot_guard_clamp_threshold(2));
    TEST_ASSERT_EQUAL_INT32(10, boot_guard_clamp_threshold(10));
    TEST_ASSERT_EQUAL_INT32(7, boot_guard_clamp_threshold(7));
}
