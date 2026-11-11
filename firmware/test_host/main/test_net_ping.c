#include <stdint.h>
#include <string.h>
#include "unity.h"
#include "net.h"

TEST_CASE("all replied: correct stats", "[net]")
{
    net_ping_stats_t s;
    int32_t rtts[] = {10, 20, 30, 40};
    net_ping_summarise(rtts, 4, &s);

    TEST_ASSERT_EQUAL_INT32(4, s.sent);
    TEST_ASSERT_EQUAL_INT32(4, s.received);
    TEST_ASSERT_EQUAL_INT32(0, s.loss_pct);
    TEST_ASSERT_EQUAL_INT32(10, s.rtt_min_ms);
    TEST_ASSERT_EQUAL_INT32(25, s.rtt_avg_ms);
    TEST_ASSERT_EQUAL_INT32(40, s.rtt_max_ms);
}

TEST_CASE("partial loss: stats over survivors only", "[net]")
{
    net_ping_stats_t s;
    int32_t rtts[] = {5, -1, 15, -1};
    net_ping_summarise(rtts, 4, &s);

    TEST_ASSERT_EQUAL_INT32(4, s.sent);
    TEST_ASSERT_EQUAL_INT32(2, s.received);
    TEST_ASSERT_EQUAL_INT32(50, s.loss_pct);
    TEST_ASSERT_EQUAL_INT32(5, s.rtt_min_ms);
    TEST_ASSERT_EQUAL_INT32(10, s.rtt_avg_ms);
    TEST_ASSERT_EQUAL_INT32(15, s.rtt_max_ms);
}

TEST_CASE("100 % loss: all rtt fields zero", "[net]")
{
    net_ping_stats_t s;
    int32_t rtts[] = {-1, -1, -1, -1};
    net_ping_summarise(rtts, 4, &s);

    TEST_ASSERT_EQUAL_INT32(4, s.sent);
    TEST_ASSERT_EQUAL_INT32(0, s.received);
    TEST_ASSERT_EQUAL_INT32(100, s.loss_pct);
    TEST_ASSERT_EQUAL_INT32(0, s.rtt_min_ms);
    TEST_ASSERT_EQUAL_INT32(0, s.rtt_avg_ms);
    TEST_ASSERT_EQUAL_INT32(0, s.rtt_max_ms);
}

TEST_CASE("count 0: all fields zero, no crash", "[net]")
{
    net_ping_stats_t s;
    net_ping_summarise(NULL, 0, &s);

    TEST_ASSERT_EQUAL_INT32(0, s.sent);
    TEST_ASSERT_EQUAL_INT32(0, s.received);
    TEST_ASSERT_EQUAL_INT32(0, s.loss_pct);
    TEST_ASSERT_EQUAL_INT32(0, s.rtt_min_ms);
    TEST_ASSERT_EQUAL_INT32(0, s.rtt_avg_ms);
    TEST_ASSERT_EQUAL_INT32(0, s.rtt_max_ms);
}

TEST_CASE("loss_pct rounding: 1 of 3 is 33, 2 of 3 is 67", "[net]")
{
    net_ping_stats_t s;
    int32_t rtts1[] = {-1, 5, 5};
    net_ping_summarise(rtts1, 3, &s);
    TEST_ASSERT_EQUAL_INT32(33, s.loss_pct);

    int32_t rtts2[] = {5, -1, -1};
    net_ping_summarise(rtts2, 3, &s);
    TEST_ASSERT_EQUAL_INT32(67, s.loss_pct);
}
