/* Pins the frozen line limits so an edit to `link.h` fails the host test
 * (`04-protocol.md` §1, `00-overview.md` §6: 4 KiB device RX, 16 KiB device TX). */
#include "unity.h"

#include "link.h"

TEST_CASE("link line caps are the frozen protocol limits", "[link]")
{
    TEST_ASSERT_EQUAL_INT(4096, LINK_RX_MAX);   /* `04-protocol.md` §1 */
    TEST_ASSERT_EQUAL_INT(16384, LINK_TX_MAX);  /* `04-protocol.md` §1 */
}
