
#include <memory>

#include "flat_placement_types.h"
#include "initial_placement.h"
#include "noc_place_utils.h"
#include "pack.h"
#include "vpr_context.h"
#include "vtr_assert.h"
#include "vtr_log.h"
#include "vtr_time.h"
#include "vpr_types.h"
#include "vpr_utils.h"

#include "globals.h"
#include "place.h"
#include "annealer.h"
#include "echo_files.h"
#include "PlacementDelayModelCreator.h"

// [PHASE 13] C++ Native Autonomous Engine
#include "autonomous_engine.h"
#include <unistd.h>
#include <sys/wait.h>
#include <sys/mman.h>
#include <algorithm>
#include <cstdio>

#include "placer.h"
#include "read_place.h"
#include "read_options.h"

/********************* Static subroutines local to place.c *******************/
#ifdef VERBOSE
void print_clb_placement(const char* fname);
#endif

/*****************************************************************************/
void try_place(const Netlist<>& net_list,
               const t_placer_opts& placer_opts,
               const t_router_opts& router_opts,
               const t_crr_opts& crr_opts,
               const t_analysis_opts& analysis_opts,
               const t_noc_opts& noc_opts,
               const t_chan_width_dist& chan_width_dist,
               t_det_routing_arch& det_routing_arch,
               const std::vector<t_segment_inf>& segment_inf,
               const std::vector<t_direct_inf>& directs,
               const FlatPlacementInfo& flat_placement_info,
               bool is_flat) {

    // Currently, the functions that require is_flat as their parameter and are called during placement should
    // receive is_flat as false. For example, if the RR graph of router lookahead is built here, it should be as
    // if is_flat is false, even if is_flat is set to true from the command line
    VTR_ASSERT(!is_flat);
    const DeviceContext& device_ctx = g_vpr_ctx.device();
    const ClusteringContext& cluster_ctx = g_vpr_ctx.clustering();
    const AtomContext& atom_ctx = g_vpr_ctx.atom();
    PlacementContext& mutable_placement = g_vpr_ctx.mutable_placement();
    FloorplanningContext& mutable_floorplanning = g_vpr_ctx.mutable_floorplanning();

    // Initialize the variables in the placement context.
    mutable_placement.init_placement_context(placer_opts, directs);

    // Re-initialize cluster constraints if erased by a previous placement run.
    // This ensures constraints are available when iterating to find the minimum channel width.
    if (mutable_floorplanning.cluster_constraints.empty()) {
        mutable_floorplanning.update_floorplanning_context_post_pack();
    }

    // Update the floorplanning constraints with the macro information from the
    // placement context.
    mutable_floorplanning.update_floorplanning_context_pre_place(*mutable_placement.place_macros);

    VTR_LOG("\n");
    VTR_LOG("Bounding box mode is %s\n", (mutable_placement.cube_bb ? "Cube" : "Per-layer"));
    VTR_LOG("\n");

    // To make sure the importance of NoC-related cost terms compared to
    // BB and timing cost is determine only through NoC placement weighting factor,
    // we normalize NoC-related cost weighting factors so that they add up to 1.
    // With this normalization, NoC-related cost weighting factors only determine
    // the relative importance of NoC cost terms with respect to each other, while
    // the importance of total NoC cost to conventional placement cost is determined
    // by NoC placement weighting factor.
    // FIXME: This should not be modifying the NoC Opts here, this normalization
    //        should occur when these Opts are loaded in.
    if (noc_opts.noc) {
        normalize_noc_cost_weighting_factor(const_cast<t_noc_opts&>(noc_opts));
    }

    // Placement delay model is independent of the placement and can be shared across
    // multiple placers if we are performing parallel annealing.
    // So, it is created and initialized once. */
    std::shared_ptr<PlaceDelayModel> place_delay_model;

    if (placer_opts.place_algorithm.is_timing_driven()) {
        /*do this before the initial placement to avoid messing up the initial placement */
        place_delay_model = PlacementDelayModelCreator::create_delay_model(placer_opts,
                                                                           router_opts,
                                                                           crr_opts,
                                                                           net_list,
                                                                           det_routing_arch,
                                                                           segment_inf,
                                                                           chan_width_dist,
                                                                           directs,
                                                                           is_flat);

        if (isEchoFileEnabled(E_ECHO_PLACEMENT_DELTA_DELAY_MODEL)) {
            place_delay_model->dump_echo(getEchoFileName(E_ECHO_PLACEMENT_DELTA_DELAY_MODEL));
        }
    }

    // Make the global instance of BlkLocRegistry inaccessible through the getter methods of the
    // placement context. This is done to make sure that the placement stage only accesses its
    // own local instances of BlkLocRegistry.
    mutable_placement.lock_loc_vars();

    // Start measuring placement time. The measured execution time will be printed
    // when this object goes out of scope at the end of this function.
    vtr::ScopedStartFinishTimer placement_timer("Placement");

    // Enables fast look-up pb graph pins from block pin indices
    IntraLbPbPinLookup pb_gpin_lookup(device_ctx.logical_block_types);
    // Enables fast look-up of atom pins connect to CLB pins
    ClusteredPinAtomPinsLookup netlist_pin_lookup(cluster_ctx.clb_nlist, atom_ctx.netlist(), pb_gpin_lookup);

    if (placer_opts.autonomous) {
        VTR_LOG("\n>>> ENTERING AUTONOMOUS SEARCH-EXPLOIT TOURNAMENT (C++ NATIVE) <<<\n");
        
        // --- DYNAMIC SCALING (Logic from Phase 12.1) ---
        // Initialize Stage 1 limit and target
        int scout_limit = 50;
        float scout_success_target = 0.35f;

        // [PHASE 15] Scale-aware limits matching Python prototype
        size_t num_blocks = net_list.blocks().size();
        if (num_blocks < 2000) {
            scout_limit = 15;
            scout_success_target = 0.50f;
        } else if (num_blocks < 10000) {
            scout_limit = 25;
            scout_success_target = 0.45f;
        }
        
        VTR_LOG("    [STAGE 1] Searching Elite 16 (Limit=%d, SuccessTarget=%.2f)...\n", scout_limit, scout_success_target);

        // --- STAGE 1: Search Portfolio (Parallel via fork) ---
        VTR_LOG("    [STAGE 1] Searching Elite 16 configs in-parallel (fork)...\n");
        size_t shm_size_s1 = ELITE_16_PORTFOLIO.size() * sizeof(t_tournament_result);
        t_tournament_result* shm_results_s1 = (t_tournament_result*)mmap(NULL, shm_size_s1, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANONYMOUS, -1, 0);

        std::fflush(stdout);
        extern double g_adaptive_momentum_scaler;
        for (size_t i = 0; i < ELITE_16_PORTFOLIO.size(); ++i) {
            if (fork() == 0) { // Child
                const auto& config = ELITE_16_PORTFOLIO[i];
                t_placer_opts scout_opts = placer_opts;
                scout_opts.prob_inject_lambda = config.lambda;
                scout_opts.prob_timing_beta = config.beta;
                scout_opts.prob_timing_alpha = config.alpha;
                scout_opts.prob_slack_gate = config.gate;
                scout_opts.prob_momentum_boost = config.boost;
                
                // Force-enable Autonomous Physics
                scout_opts.prob_timing_inject = true;
                scout_opts.prob_inject_mode = "regularize";
                scout_opts.prob_timing_enable = true;
                scout_opts.prob_timing_mode = 4;
                scout_opts.place_algorithm = e_place_algorithm::CRITICALITY_TIMING_PLACE;
                
                // [PHASE 15] Universal breakthrough: Fast Schedule & Clean Init
                scout_opts.place_auto_init_t_scale = 0.1; 
                scout_opts.anneal_sched.alpha_t = 0.70;

                scout_opts.scout_limit = scout_limit;
                scout_opts.scout_success_target = scout_success_target;
                
                Placer scout_placer(net_list, {}, scout_opts, analysis_opts, noc_opts, pb_gpin_lookup, netlist_pin_lookup,
                                    flat_placement_info, place_delay_model, scout_opts.place_auto_init_t_scale,
                                    mutable_placement.cube_bb, is_flat, /*quiet=*/true);
                double initial_timing_cost = scout_placer.costs().timing_cost;
                scout_placer.place();
                
                double gain = (initial_timing_cost - scout_placer.costs().timing_cost);
                double score = gain * g_adaptive_momentum_scaler;

                shm_results_s1[i] = t_tournament_result(config.id, 1, scout_placer.costs().cost, (double)scout_placer.critical_path().delay(), score, true);
                _exit(0);
            }
        }
        // Wait for all Stage 1 children
        for (size_t i = 0; i < ELITE_16_PORTFOLIO.size(); ++i) wait(NULL);
        
        std::vector<t_tournament_result> scout_results;
        for (size_t i = 0; i < ELITE_16_PORTFOLIO.size(); ++i) {
            if (shm_results_s1[i].config_id != 0) scout_results.push_back(shm_results_s1[i]);
        }
        munmap(shm_results_s1, shm_size_s1);
        
        VTR_LOG("    [STAGE 1] COMPLETE. Picking winners...\n");
        std::sort(scout_results.begin(), scout_results.end(), [](const t_tournament_result& a, const t_tournament_result& b) {
            return a.pb_score > b.pb_score; // Higher PBScore is better
        });
        
        int winner1_id = scout_results[0].config_id;
        int winner2_id = scout_results[1].config_id;
        VTR_LOG("    Winners: Config %d (Cost=%.2f), Config %d (Cost=%.2f)\n", winner1_id, scout_results[0].cost, winner2_id, scout_results[1].cost);

        // --- STAGE 2: Breakout Attack (Parallel via fork) ---
        VTR_LOG("    [STAGE 2] Exploiting Top 2 Winners (8 seeds each) in-parallel (fork)...\n");
        size_t shm_size_s2 = 16 * sizeof(t_tournament_result);
        t_tournament_result* shm_results_s2 = (t_tournament_result*)mmap(NULL, shm_size_s2, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANONYMOUS, -1, 0);

        std::fflush(stdout);
        for (int s = 1; s <= 8; ++s) {
            if (fork() == 0) { // Winner 1 Child
                t_placer_opts exploit_opts = placer_opts;
                auto it = std::find_if(ELITE_16_PORTFOLIO.begin(), ELITE_16_PORTFOLIO.end(), [winner1_id](const t_autonomous_config& c) { return c.id == winner1_id; });
                const auto& config = *it;
                exploit_opts.prob_inject_lambda = config.lambda;
                exploit_opts.prob_timing_beta = config.beta;
                exploit_opts.prob_timing_alpha = config.alpha;
                exploit_opts.prob_slack_gate = config.gate;
                exploit_opts.prob_momentum_boost = config.boost;

                // Force-enable Autonomous Physics
                exploit_opts.prob_timing_inject = true;
                exploit_opts.prob_inject_mode = "regularize";
                exploit_opts.prob_timing_enable = true;
                exploit_opts.prob_timing_mode = 4;
                exploit_opts.place_algorithm = e_place_algorithm::CRITICALITY_TIMING_PLACE;

                // [PHASE 15] Universal breakthrough: Fast Schedule & Clean Init
                exploit_opts.place_auto_init_t_scale = 0.1;
                exploit_opts.anneal_sched.alpha_t = 0.70;

                exploit_opts.seed = s;
                exploit_opts.scout_limit = 0;
                
                Placer exploit_placer(net_list, {}, exploit_opts, analysis_opts, noc_opts, pb_gpin_lookup, netlist_pin_lookup,
                                      flat_placement_info, place_delay_model, exploit_opts.place_auto_init_t_scale,
                                      mutable_placement.cube_bb, is_flat, /*quiet=*/true);
                exploit_placer.place();
                shm_results_s2[s-1] = t_tournament_result(winner1_id, s, exploit_placer.costs().cost, (double)exploit_placer.critical_path().delay(), 0.0, true);
                
                // Write placement to temp file for sync
                std::string tmp_file = vtr::string_fmt("autonomous_temp_%d_%d.place", winner1_id, s);
                print_place("auto_net", "auto_id", tmp_file.c_str(), exploit_placer.mutable_state().block_locs());
                
                _exit(0);
            }
            std::fflush(stdout);
            if (fork() == 0) { // Winner 2 Child
                t_placer_opts exploit_opts = placer_opts;
                auto it = std::find_if(ELITE_16_PORTFOLIO.begin(), ELITE_16_PORTFOLIO.end(), [winner2_id](const t_autonomous_config& c) { return c.id == winner2_id; });
                const auto& config = *it;
                exploit_opts.prob_inject_lambda = config.lambda;
                exploit_opts.prob_timing_beta = config.beta;
                exploit_opts.prob_timing_alpha = config.alpha;
                exploit_opts.prob_slack_gate = config.gate;
                exploit_opts.prob_momentum_boost = config.boost;

                // Force-enable Autonomous Physics
                exploit_opts.prob_timing_inject = true;
                exploit_opts.prob_inject_mode = "regularize";
                exploit_opts.prob_timing_enable = true;
                exploit_opts.prob_timing_mode = 4;
                exploit_opts.place_algorithm = e_place_algorithm::CRITICALITY_TIMING_PLACE;

                // [PHASE 15] Universal breakthrough: Fast Schedule & Clean Init
                exploit_opts.place_auto_init_t_scale = 0.1;
                exploit_opts.anneal_sched.alpha_t = 0.70;

                exploit_opts.seed = s;
                exploit_opts.scout_limit = 0;
                
                Placer exploit_placer(net_list, {}, exploit_opts, analysis_opts, noc_opts, pb_gpin_lookup, netlist_pin_lookup,
                                      flat_placement_info, place_delay_model, exploit_opts.place_auto_init_t_scale,
                                      mutable_placement.cube_bb, is_flat, /*quiet=*/true);
                exploit_placer.place();
                shm_results_s2[8+s-1] = t_tournament_result(winner2_id, s, exploit_placer.costs().cost, (double)exploit_placer.critical_path().delay(), 0.0, true);

                // Write placement to temp file for sync
                std::string tmp_file = vtr::string_fmt("autonomous_temp_%d_%d.place", winner2_id, s);
                print_place("auto_net", "auto_id", tmp_file.c_str(), exploit_placer.mutable_state().block_locs());

                _exit(0);
            }
        }
        // Wait for all Stage 2 children
        for (int i = 0; i < 16; ++i) wait(NULL);

        t_tournament_result best_final;
        for (int i = 0; i < 16; ++i) {
            if (shm_results_s2[i].cpd < best_final.cpd) best_final = shm_results_s2[i];
        }
        munmap(shm_results_s2, shm_size_s2);
        
        VTR_LOG("    [STAGE 2] COMPLETE. Best Result: CPD=%.3f ns (Winner %d, Seed %d). Syncing...\n", best_final.cpd, best_final.config_id, best_final.seed);

        // --- FINAL COMMITTAL: Load placement from file to update global state ---
        std::string final_tmp_file = vtr::string_fmt("autonomous_temp_%d_%d.place", best_final.config_id, best_final.seed);
        
        // Use a dummy placer to hold the state, then update global
        t_placer_opts sync_opts = placer_opts;
        sync_opts.scout_limit = 0;
        Placer sync_placer(net_list, {}, sync_opts, analysis_opts, noc_opts, pb_gpin_lookup, netlist_pin_lookup,
                            flat_placement_info, place_delay_model, placer_opts.place_auto_init_t_scale,
                            mutable_placement.cube_bb, is_flat, /*quiet=*/true);
        
        read_place("auto_net", final_tmp_file.c_str(), sync_placer.mutable_state().mutable_blk_loc_registry(), /*verify_hashes=*/false, g_vpr_ctx.device().grid);
        sync_placer.update_global_state();

        // Cleanup temp files
        for (int i=1; i<=16; ++i) {
             // We don't bother individual names, just try cleanup best or all?
        }
        system("rm -f autonomous_temp_*.place");
    } else {
        Placer placer(net_list, {}, placer_opts, analysis_opts, noc_opts, pb_gpin_lookup, netlist_pin_lookup,
                      flat_placement_info, place_delay_model, placer_opts.place_auto_init_t_scale,
                      mutable_placement.cube_bb, is_flat, /*quiet=*/false);

        placer.place();

        // The placer object has its own copy of block locations and doesn't update
        // the global context directly. We need to copy its internal data structures
        // to the global placement context before it goes out of scope.
        placer.update_global_state();
    }

    // Clean the variables in the placement context. This will deallocate memory
    // used by variables which were allocated in the placement context and are
    // never used outside of placement.
    mutable_placement.clean_placement_context_post_place();
    mutable_floorplanning.clean_floorplanning_context_post_place();
}

#ifdef VERBOSE
void print_clb_placement(const char* fname) {
    /* Prints out the clb placements to a file.  */
    FILE* fp;
    auto& cluster_ctx = g_vpr_ctx.clustering();
    auto& place_ctx = g_vpr_ctx.placement();

    fp = vtr::fopen(fname, "w");
    fprintf(fp, "Complex block placements:\n\n");

    fprintf(fp, "Block #\tName\t(X, Y, Z).\n");
    for (auto i : cluster_ctx.clb_nlist.blocks()) {
        fprintf(fp, "#%d\t%s\t(%d, %d, %d).\n", i, cluster_ctx.clb_nlist.block_name(i).c_str(), place_ctx.block_locs[i].loc.x, place_ctx.block_locs[i].loc.y, place_ctx.block_locs[i].loc.sub_tile);
    }

    fclose(fp);
}
#endif

#if 0
static void update_screen_debug();

//Performs a major (i.e. interactive) placement screen update.
//This function with no arguments is useful for calling from a debugger to
//look at the intermediate implementation state.
static void update_screen_debug() {
    update_screen(ScreenUpdatePriority::MAJOR, "DEBUG", e_pic_type::PLACEMENT, nullptr);
}
#endif
