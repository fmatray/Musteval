#pragma once

#include "link.h"

/* Reader-side accessor for the slice that owns the CDC read task;
 * `link_set_sink` stores it, this returns it. */
const link_sink_t *link_sink_get(void);
