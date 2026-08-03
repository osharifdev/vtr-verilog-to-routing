#ifndef V0_PLACEMENT_H
#define V0_PLACEMENT_H

#include "vpr_types.h"
#include "place_macro.h"
#include "blk_loc_registry.h"

/**
 * @brief Entry point for V0 deterministic initial placement.
 * 
 * Replaces random initial placement with an inference-driven barycenter strategy.
 * This function performs a Logical-STA-Pass, calculates net weights (omega),
 * sorts blocks by inferred criticality, and sequentially places blocks at their
 * barycenters using a deterministic spiral search for legalization.
 * 
 * @param placer_opts VPR placement options (contains v0_enable, prob_config_id, etc.)
 * @param blk_loc_registry Registry to store the generated placement
 * @param place_macros Macro constraints (V0 will fail if macros are present)
 */
void run_v0_placement(const t_placer_opts& placer_opts,
                      BlkLocRegistry& blk_loc_registry,
                      const PlaceMacros& place_macros);

#endif
