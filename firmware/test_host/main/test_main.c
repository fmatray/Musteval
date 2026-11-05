/* Non-interactive unity entry (`AGENTS.md` Test Strategy): run every registered
 * test once and exit with the failure count so CI gets a status code. */
#include <stdlib.h>

#include "unity.h"

void app_main(void)
{
    UNITY_BEGIN();
    unity_run_all_tests();
    /* UnityEnd returns the failure count; exit status makes failures visible to CI. */
    exit(UNITY_END() == 0 ? EXIT_SUCCESS : EXIT_FAILURE);
}
