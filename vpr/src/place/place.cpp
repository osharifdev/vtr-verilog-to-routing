
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

    // PROXY 1.2+: Skip delta delay model when proxy level >= 2 and stop_after >= 0
    bool proxy_skip_delay_model = (placer_opts.proxy_checkpoint_enable
                                    && placer_opts.proxy_checkpoint_level >= 2
                                    && placer_opts.proxy_checkpoint_stop_after >= 0);
    if (placer_opts.place_algorithm.is_timing_driven() && !proxy_skip_delay_model) {
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
    } else if (proxy_skip_delay_model) {
        VTR_LOG("PROXY 1.2: Skipping delta delay model computation (level=%d)\n",
                placer_opts.proxy_checkpoint_level);
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

    // size_t num_blocks = net_list.blocks().size(); // Removed unused variable
    
    if (placer_opts.autonomous) {
        VTR_LOG("\n>>> ENTERING SEARCH-64 AUTONOMOUS TOURNAMENT <<<\n");
        VTR_LOG("    [PHYSICS] Dynamic lambda annealing (noise fades with temperature) active.\n");

        size_t shm_size = 64 * sizeof(t_tournament_result);
        t_tournament_result* shm_results = (t_tournament_result*)mmap(NULL, shm_size, PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANONYMOUS, -1, 0);
        if (shm_results == MAP_FAILED) {
            VTR_LOG_ERROR("Failed to allocate shared memory for autonomous tournament.\n");
            return;
        }
        for (int i=0; i<64; ++i) shm_results[i].success = false;

        VTR_LOG("    Launching 64 Parallel Scouts (Full Placement)...\n");
        std::fflush(stdout);

        for (int i = 0; i < 64; ++i) {
            if (fork() == 0) {
                t_placer_opts scout_opts = placer_opts;
                const auto& config = ELITE_16_PORTFOLIO[i % 16];
                scout_opts.prob_inject_lambda = config.lambda;
                scout_opts.prob_timing_beta = config.beta;
                scout_opts.prob_timing_alpha = config.alpha;
                scout_opts.prob_slack_gate = config.gate;
                scout_opts.prob_momentum_boost = config.boost;
                scout_opts.seed = (i + 1) * 789; // Diverse seeds

                // Force-enable Autonomous Physics
                scout_opts.prob_timing_inject = true;
                scout_opts.prob_inject_mode = "regularize";
                scout_opts.prob_timing_enable = true;
                scout_opts.prob_timing_mode = 4;
                scout_opts.place_algorithm = e_place_algorithm::CRITICALITY_TIMING_PLACE;

                scout_opts.autonomous_is_scout = true;
                scout_opts.shm_results_ptr = (void*)shm_results;
                scout_opts.autonomous_worker_id = i;
                
                // Diversify annealing speed slightly (0.7 to 0.8)
                scout_opts.anneal_sched.alpha_t = 0.70 + (float)(i % 5) * 0.02;

                Placer scout_placer(net_list, {}, scout_opts, analysis_opts, noc_opts, pb_gpin_lookup, netlist_pin_lookup,
                                      flat_placement_info, place_delay_model, scout_opts.place_auto_init_t_scale,
                                      mutable_placement.cube_bb, is_flat, /*quiet=*/true);
                scout_placer.place();

                double final_cpd = 1e9 * (double)scout_placer.critical_path().delay();
                shm_results[i] = t_tournament_result(config.id, i, scout_placer.costs().cost, final_cpd, 0.0, true);
                
                // Save placement to worker-specific file
                std::string tmp_file = vtr::string_fmt("auto_worker_%d.place", i);
                print_place("auto", "auto", tmp_file.c_str(), scout_placer.mutable_state().block_locs());

                _exit(0);
            }
        }

        for (int i = 0; i < 64; ++i) wait(NULL);

        VTR_LOG("    Search Complete. Selecting best placement...\n");
        double best_cpd = std::numeric_limits<double>::infinity();
        int best_worker = -1;
        for (int i = 0; i < 64; ++i) {
            if (shm_results[i].success && shm_results[i].cpd < best_cpd) {
                best_cpd = shm_results[i].cpd;
                best_worker = i;
            }
        }

        if (best_worker != -1) {
            VTR_LOG("    WINNER: Worker %d, CPD=%.4f ns\n", best_worker, best_cpd);
            std::string win_file = vtr::string_fmt("auto_worker_%d.place", best_worker);
            
            // The placement location variables should be unlocked before being accessed
            auto& placement_ctx = g_vpr_ctx.mutable_placement();
            placement_ctx.unlock_loc_vars();
            read_place("auto.net", win_file.c_str(), placement_ctx.mutable_blk_loc_registry(), false, device_ctx.grid);
            placement_ctx.lock_loc_vars();
        } else {
            VTR_LOG_ERROR("Autonomous search failed to produce any valid results.\n");
        }

        // Cleanup
        for (int i = 0; i < 64; ++i) {
            std::string tmp = vtr::string_fmt("auto_worker_%d.place", i);
            std::remove(tmp.c_str());
        }
        munmap(shm_results, shm_size);
        
        VTR_LOG(">>> AUTONOMOUS SEARCH COMPLETE <<<\n\n");
        return;
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
