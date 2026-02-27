
#include "placer.h"

#include <functional>
#include <optional>
#include <utility>

#include "echo_files.h"
#include "flat_placement_types.h"
#include "blk_loc_registry.h"
#include "place_macro.h"
#include "vtr_time.h"
#include "draw.h"
#include "read_place.h"
#include "initial_placement.h"
#include "load_flat_place.h"
#include "concrete_timing_info.h"
#include "verify_placement.h"
#include "place_timing_update.h"
#include "annealer.h"
#include "RL_agent_util.h"
#include "place_checkpoint.h"
#include "raiga.h"
#include "timing/PlacerCriticalities.h"
#include "tatum/echo_writer.hpp"

#ifndef NO_GRAPHICS
#include "draw_global.h"
#endif // NO_GRAPHICS

Placer::Placer(const Netlist<>& net_list,
               std::optional<std::reference_wrapper<const BlkLocRegistry>> init_place,
               const t_placer_opts& placer_opts,
               const t_analysis_opts& analysis_opts,
               const t_noc_opts& noc_opts,
               const t_router_opts& router_opts,
               const t_crr_opts& crr_opts,
               const t_chan_width_dist& chan_width_dist,
               const t_det_routing_arch& det_routing_arch,
               const std::vector<t_segment_inf>& segment_inf,
               const std::vector<t_direct_inf>& directs,
               const IntraLbPbPinLookup& pb_gpin_lookup,
               const ClusteredPinAtomPinsLookup& netlist_pin_lookup,
               const FlatPlacementInfo& flat_placement_info,
               std::shared_ptr<PlaceDelayModel> place_delay_model,
               float anneal_auto_init_t_scale,
               bool cube_bb,
               bool is_flat,
               bool quiet)
    : placer_opts_(placer_opts)
    , analysis_opts_(analysis_opts)
    , noc_opts_(noc_opts)
    , router_opts_(router_opts)
    , crr_opts_(crr_opts)
    , chan_width_dist_(chan_width_dist)
    , det_routing_arch_(det_routing_arch)
    , segment_inf_(segment_inf)
    , directs_(directs)
    , pb_gpin_lookup_(pb_gpin_lookup)
    , netlist_pin_lookup_(netlist_pin_lookup)
    , costs_(placer_opts.place_algorithm, noc_opts.noc)
    , placer_state_(placer_opts.place_algorithm.is_timing_driven())
    , rng_(placer_opts.seed)
    , net_cost_handler_(placer_opts, placer_state_, cube_bb)
    , place_delay_model_(std::move(place_delay_model))
    , log_printer_(*this, quiet)
    , quench_only_(placer_opts.place_quench_only)
    , is_flat_(is_flat) {
    VTR_LOG("  [DEBUG] Entering Placer constructor\n");
    fflush(stdout);
    const auto& cluster_ctx = g_vpr_ctx.clustering();

    pre_place_timing_stats_ = g_vpr_ctx.timing().stats;

    const PlaceMacros& place_macros = *g_vpr_ctx.placement().place_macros;

    // create a NoC cost handler if NoC optimization is enabled
    if (noc_opts.noc) {
        noc_cost_handler_.emplace(placer_state_.block_locs());
    }

    // Initialize the placement for the Simulated Annealer.
    BlkLocRegistry& blk_loc_registry = placer_state_.mutable_blk_loc_registry();
    if (init_place.has_value()) {
        // If an initial placement has been provided, use that.
        blk_loc_registry = *init_place;
    } else {
        // If an initial placement has not been provided, run the initial placer.
        initial_placement(placer_opts, placer_opts.constraints_file.c_str(),
                          noc_opts, blk_loc_registry, place_macros, noc_cost_handler_,
                          flat_placement_info, rng_);

        // After initial placement, if a flat placement is being reconstructed,
        // print flat placement reconstruction info.
        if (flat_placement_info.valid) {
            log_flat_placement_reconstruction_info(flat_placement_info,
                                                   blk_loc_registry.block_locs(),
                                                   g_vpr_ctx.clustering().atoms_lookup,
                                                   g_vpr_ctx.atom().lookup(),
                                                   g_vpr_ctx.atom().netlist(),
                                                   g_vpr_ctx.clustering().clb_nlist);
        }
    }

    const int move_lim = (int)(placer_opts.anneal_sched.inner_num * pow(net_list.blocks().size(), 1.3333));
    //create the move generator based on the chosen placement strategy
    auto [move_generator, move_generator2] = create_move_generators(placer_state_,
                                                                    place_macros,
                                                                    net_cost_handler_,
                                                                    placer_opts, move_lim,
                                                                    noc_opts.noc_centroid_weight,
                                                                    rng_);

    if (!placer_opts.write_initial_place_file.empty()) {
        print_place(nullptr, nullptr, placer_opts.write_initial_place_file.c_str(), placer_state_.block_locs());
    }

    // Update physical pin values
    for (const ClusterBlockId block_id : cluster_ctx.clb_nlist.blocks()) {
        blk_loc_registry.place_sync_external_block_connections(block_id);
    }

    if (!quiet) {
#ifndef NO_GRAPHICS
        if (noc_cost_handler_.has_value()) {
            get_draw_state_vars()->set_noc_link_bandwidth_usages_ref(noc_cost_handler_->get_link_bandwidth_usages());
        }
#endif

        // width_fac gives the width of the widest channel
        const int width_fac = placer_opts.place_chan_width;
        init_draw_coords((float)width_fac, placer_state_.blk_loc_registry());
    }

    // Gets initial cost and loads bounding boxes.
    std::tie(costs_.bb_cost, std::ignore, costs_.congestion_cost) = net_cost_handler_.comp_bb_cong_cost(e_cost_methods::NORMAL);

    if (placer_opts.place_algorithm.is_timing_driven()) {
        alloc_and_init_timing_objects_(net_list, analysis_opts);
    } else {
        VTR_ASSERT(placer_opts.place_algorithm == e_place_algorithm::BOUNDING_BOX_PLACE);
        // Timing cost is not used
        costs_.timing_cost = std::numeric_limits<double>::quiet_NaN();
    }

    costs_.update_norm_factors();

    if (noc_opts.noc) {
        VTR_ASSERT(noc_cost_handler_.has_value());

        // get the costs associated with the NoC
        costs_.noc_cost_terms.aggregate_bandwidth = noc_cost_handler_->comp_noc_aggregate_bandwidth_cost();
        std::tie(costs_.noc_cost_terms.latency, costs_.noc_cost_terms.latency_overrun) = noc_cost_handler_->comp_noc_latency_cost();
        costs_.noc_cost_terms.congestion = noc_cost_handler_->comp_noc_congestion_cost();

        // initialize all the noc normalization factors
        noc_cost_handler_->update_noc_normalization_factors(costs_);
    }

    // set the starting total placement cost
    costs_.cost = costs_.get_total_cost(placer_opts, noc_opts);

    // Sanity check that initial placement is legal
    check_place_();

    log_printer_.print_initial_placement_stats();

    annealer_ = std::make_unique<PlacementAnnealer>(placer_opts_, placer_state_, place_macros, costs_, net_cost_handler_, noc_cost_handler_,
                                                    noc_opts_, rng_, std::move(move_generator), std::move(move_generator2), place_delay_model_.get(),
                                                    placer_criticalities_.get(), placer_setup_slacks_.get(), timing_info_.get(), pin_timing_invalidator_.get(),
                                                    anneal_auto_init_t_scale,
                                                    move_lim);
}

void Placer::alloc_and_init_timing_objects_(const Netlist<>& net_list,
                                            const t_analysis_opts& analysis_opts) {
    const auto& atom_ctx = g_vpr_ctx.atom();
    const auto& cluster_ctx = g_vpr_ctx.clustering();
    const auto& timing_ctx = g_vpr_ctx.timing();
    const auto& p_timing_ctx = placer_state_.timing();

    // Update the point-to-point delays from the initial placement
    comp_td_connection_delays(place_delay_model_.get(), placer_state_);

    // Initialize timing analysis
    placement_delay_calc_ = std::make_shared<PlacementDelayCalculator>(atom_ctx.netlist(),
                                                                       atom_ctx.lookup(),
                                                                       p_timing_ctx.connection_delay,
                                                                       is_flat_);
    placement_delay_calc_->set_tsu_margin_relative(placer_opts_.tsu_rel_margin);
    placement_delay_calc_->set_tsu_margin_absolute(placer_opts_.tsu_abs_margin);

    timing_info_ = make_setup_timing_info(placement_delay_calc_, placer_opts_.timing_update_type);

    placer_setup_slacks_ = std::make_unique<PlacerSetupSlacks>(cluster_ctx.clb_nlist,
                                                               netlist_pin_lookup_,
                                                               timing_info_);

    placer_criticalities_ = std::make_unique<PlacerCriticalities>(cluster_ctx.clb_nlist,
                                                                  netlist_pin_lookup_,
                                                                  timing_info_);

    pin_timing_invalidator_ = make_net_pin_timing_invalidator(placer_opts_.timing_update_type,
                                                              net_list,
                                                              netlist_pin_lookup_,
                                                              atom_ctx.netlist(),
                                                              atom_ctx.lookup(),
                                                              timing_info_,
                                                              is_flat_);

    // First time compute timing and costs, compute from scratch
    PlaceCritParams crit_params;
    crit_params.crit_exponent = placer_opts_.td_place_exp_first;
    crit_params.crit_limit = placer_opts_.place_crit_limit;
    crit_params.prob_strategy = placer_opts_.prob_timing_strategy;
    crit_params.prob_mc_samples = placer_opts_.prob_timing_mc_samples;
    crit_params.prob_risk_z = placer_opts_.prob_timing_risk_z;
    crit_params.prob_alpha = placer_opts_.prob_timing_alpha;
    crit_params.prob_beta = placer_opts_.prob_timing_beta;
    crit_params.prob_self_calibrate = placer_opts_.prob_self_calibrate;
    crit_params.prob_congestion_gamma = placer_opts_.prob_congestion_gamma;
    crit_params.prob_schedule_ramp = placer_opts_.prob_schedule_ramp;
    crit_params.prob_dist_func = placer_opts_.prob_dist_func;
    crit_params.prob_dist_threshold = placer_opts_.prob_dist_threshold;
    crit_params.prob_huber_delta = placer_opts_.prob_huber_delta;
    crit_params.prob_slack_gate = placer_opts_.prob_slack_gate;
    crit_params.prob_hallucination_dampen = placer_opts_.prob_hallucination_dampen;
    crit_params.prob_momentum_boost = placer_opts_.prob_momentum_boost;
    crit_params.net_cost_handler = &net_cost_handler_;
    crit_params.current_temp = 100.0f; // High for initial update

    initialize_timing_info(placer_opts_,
                           crit_params,
                           place_delay_model_.get(),
                           placer_criticalities_.get(),
                           placer_setup_slacks_.get(),
                           pin_timing_invalidator_.get(),
                           timing_info_.get(),
                           &costs_,
                           placer_state_);

    critical_path_ = timing_info_->least_slack_critical_path();

    // Write out the initial timing echo file
    if (isEchoFileEnabled(E_ECHO_INITIAL_PLACEMENT_TIMING_GRAPH)) {
        tatum::write_echo(getEchoFileName(E_ECHO_INITIAL_PLACEMENT_TIMING_GRAPH),
                          *timing_ctx.graph, *timing_ctx.constraints,
                          *placement_delay_calc_, timing_info_->analyzer());

        tatum::NodeId debug_tnode = id_or_pin_name_to_tnode(analysis_opts.echo_dot_timing_graph_node);

        write_setup_timing_graph_dot(getEchoFileName(E_ECHO_INITIAL_PLACEMENT_TIMING_GRAPH) + std::string(".dot"),
                                     *timing_info_, debug_tnode);
    }
}

void Placer::check_place_() {
    const ClusteredNetlist& clb_nlist = g_vpr_ctx.clustering().clb_nlist;
    const DeviceGrid& device_grid = g_vpr_ctx.device().grid;
    const auto& cluster_constraints = g_vpr_ctx.floorplanning().cluster_constraints;
    const PlaceMacros& place_macros = *g_vpr_ctx.placement().place_macros;

    int error = 0;

    // Verify the placement invariants independent to the placement flow.
    error += verify_placement(placer_state_.blk_loc_registry(),
                              place_macros,
                              clb_nlist,
                              device_grid,
                              cluster_constraints);

    error += check_placement_costs_();

    if (noc_opts_.noc) {
        // check the NoC costs during placement if the user is using the NoC supported flow
        error += noc_cost_handler_->check_noc_placement_costs(costs_, placer_opts_.place_static_cost_tolerance, noc_opts_);
        // make sure NoC routing configuration does not create any cycles in CDG
        error += (int)noc_cost_handler_->noc_routing_has_cycle();
    }

    if (error == 0) {
        VTR_LOGV(!log_printer_.quiet(),
                 "\nCompleted placement consistency check successfully.\n");

    } else {
        VPR_ERROR(VPR_ERROR_PLACE,
                  "\nCompleted placement consistency check, %d errors found.\n"
                  "Aborting program.\n",
                  error);
    }
}

int Placer::check_placement_costs_() {
    int error = 0;

    const auto [bb_cost_check, expected_wirelength, _] = net_cost_handler_.comp_bb_cong_cost(e_cost_methods::CHECK);

    if (fabs(bb_cost_check - costs_.bb_cost) > costs_.bb_cost * placer_opts_.place_static_cost_tolerance) {
        VTR_LOG_ERROR(
            "bb_cost_check: %g and bb_cost: %g differ in check_place.\n",
            bb_cost_check, costs_.bb_cost);
        error++;
    }

    if (placer_opts_.place_algorithm.is_timing_driven()) {
        double timing_cost_check;
        comp_td_costs(place_delay_model_.get(), *placer_criticalities_, placer_state_, &timing_cost_check);
        if (fabs(timing_cost_check - costs_.timing_cost) > costs_.timing_cost * placer_opts_.place_static_cost_tolerance) {
            if (placer_opts_.prob_timing_inject) {
                VTR_LOG("timing_cost_check: %g and timing_cost: %g differ in check_place, but prob_timing_inject is on. Allowing.\n",
                        timing_cost_check, costs_.timing_cost);
            } else {
                VTR_LOG_ERROR(
                    "timing_cost_check: %g and timing_cost: %g differ in check_place.\n",
                    timing_cost_check, costs_.timing_cost);
                error++;
            }
        }
    }
    return error;
}

void Placer::place() {

    fflush(stdout);
    const auto& timing_ctx = g_vpr_ctx.timing();
    const auto& cluster_ctx = g_vpr_ctx.clustering();
    bool analytic_place_enabled = false;

    if (!analytic_place_enabled && !quench_only_) {
        // Table header
        log_printer_.print_place_status_header();

        // Outer loop of the simulated annealing begins
        do {

            vtr::Timer temperature_timer;


            annealer_->outer_loop_update_timing_info();

            if (placer_opts_.place_algorithm.is_timing_driven()) {
                critical_path_ = timing_info_->least_slack_critical_path();

                // see if we should save the current placement solution as a checkpoint
                if (placer_opts_.place_checkpointing && annealer_->get_agent_state() == e_agent_state::LATE_IN_THE_ANNEAL) {
                    save_placement_checkpoint_if_needed(placer_state_.mutable_block_locs(),
                                                        placement_checkpoint_,
                                                        timing_info_, costs_, critical_path_.delay());
                }
            }

            // RA-IGA Step 6: Synchronize Rudy grid weights with latest criticalities
            if (placer_opts_.raiga_enable && inc_rudy_) {
                inc_rudy_->update_weights(cluster_ctx.clb_nlist, 
                                         placer_state_, 
                                         *placer_criticalities_, 
                                         placer_opts_.raiga_k);
            }

            // do a complete inner loop iteration
            annealer_->placement_inner_loop();

            if (placer_opts_.raiga_enable) {
                float p = annealer_->get_progress();
                int current_temp_count = annealer_->get_annealing_state().num_temps;
                
                // Estimate total steps based on current progress
                float dynamic_total_steps = (p > 0.001f) ? (current_temp_count / p) : 0.0f;
                int total_steps = (int)std::round(dynamic_total_steps);

                // Condensed debug print for RA-IGA progress
                if (current_temp_count % 10 == 1 || (p > 0.04f && current_temp_count % 5 == 0)) {
                    VTR_LOG("  [RAIGA_DEBUG] step=%d/%d T=%g p=%.4f S1=%.2f S2=%.2f\n", 
                            current_temp_count, total_steps, 
                            annealer_->get_annealing_state().t, p, 
                            placer_opts_.raiga_S1, placer_opts_.raiga_S2);
                    fflush(stdout);
                }

                // RA-IGA Step 3 Hook: CP1 Cheap Congestion Prune (RUDY)
                if (!raiga_cp1_done_ && p >= placer_opts_.raiga_S1) {
                    raiga_cp1_done_ = true;
                    
                    auto& device_ctx = g_vpr_ctx.device();
                    auto cp1_metrics = raiga::compute_rudy_grid(device_ctx.grid, 
                                                                cluster_ctx.clb_nlist, 
                                                                placer_state_, 
                                                                placer_opts_.raiga_rudy_theta);

                    VTR_LOG("RAIGA_CP1_RUDY,%d,%d,CP1,%d,%d,%.4f,%.4f,%.4f,%.4f,%d\n", 
                            placer_opts_.raiga_scout_id, 
                            placer_opts_.seed, 
                            current_temp_count,
                            total_steps,
                            cp1_metrics.peak, 
                            cp1_metrics.p95, 
                            cp1_metrics.hotspot_cost, 
                            cp1_metrics.theta_used,
                            cp1_metrics.num_nets_included);

                    if (!placer_opts_.raiga_log_csv.empty()) {
                        FILE* f = fopen(placer_opts_.raiga_log_csv.c_str(), "a");
                        if (f) {
                            fprintf(f, "%d,%d,CP1,%d,%d,%.4f,%.4f,%.4f,%.4f,%d\n", 
                                    placer_opts_.raiga_scout_id, 
                                    placer_opts_.seed, 
                                    current_temp_count,
                                    total_steps,
                                    cp1_metrics.peak, 
                                    cp1_metrics.p95, 
                                    cp1_metrics.hotspot_cost, 
                                    cp1_metrics.theta_used,
                                    cp1_metrics.num_nets_included);
                            fclose(f);
                        }
                    }

                    // Step 4: Initialize IncrementalRudy map
                    inc_rudy_ = std::make_unique<raiga::IncrementalRudy>();
                    inc_rudy_->init(device_ctx.grid, 
                                    cluster_ctx.clb_nlist, 
                                    placer_state_, 
                                    cp1_metrics.theta_used,
                                    placer_opts_.raiga_bins_x,
                                    placer_opts_.raiga_bins_y);
                    annealer_->set_inc_rudy(inc_rudy_.get());
                }

                // RA-IGA Step 1 Hook: CP2 Probe + Logging
                if (placer_opts_.raiga_probe_route_enable && !raiga_probe_done_ && p >= placer_opts_.raiga_S2) {
                    raiga_probe_done_ = true;
                    VTR_LOG("    [RAIGA_CP2_PROBE] Starting probe for scout_id=%d seed=%d\n", 
                            placer_opts_.raiga_scout_id, placer_opts_.seed);
                    fflush(stdout);
                    
                    update_global_state();
                    
                    auto metrics = raiga::probe_route(cluster_ctx.clb_nlist, 
                                                      router_opts_, 
                                                      crr_opts_, 
                                                      analysis_opts_, 
                                                      chan_width_dist_, 
                                                      det_routing_arch_, 
                                                      segment_inf_, 
                                                      directs_, 
                                                      is_flat_, 
                                                      placer_opts_.raiga_probe_route_max_iters, 
                                                      placer_opts_.raiga_probe_route_time_cap_s);

                    g_vpr_ctx.mutable_placement().lock_loc_vars(); // Relock AFTER probe completes

                    VTR_LOG("RAIGA_CP2_PROBE,%d,%d,%.0f,%.0f,%.3f\n", 
                            placer_opts_.raiga_scout_id, 
                            placer_opts_.seed, 
                            metrics.total_overflow, 
                            metrics.max_overflow, 
                            metrics.runtime_s);

                    if (!placer_opts_.raiga_log_csv.empty()) {
                        FILE* f = fopen(placer_opts_.raiga_log_csv.c_str(), "a");
                        if (f) {
                            fprintf(f, "%d,%d,%.0f,%.0f,%.3f\n", 
                                    placer_opts_.raiga_scout_id, 
                                    placer_opts_.seed, 
                                    metrics.total_overflow, 
                                    metrics.max_overflow, 
                                    metrics.runtime_s);
                            fclose(f);
                        }
                    }
                }
            }

            log_printer_.print_place_status(temperature_timer.elapsed_sec());

            // Outer loop of the simulated annealing ends
        } while (annealer_->outer_loop_update_state());
    } // skip_anneal ends

    // Start Quench
    annealer_->start_quench();

    pre_quench_timing_stats_ = timing_ctx.stats;
    { // Quench
        vtr::ScopedFinishTimer temperature_timer("Placement Quench");

        annealer_->outer_loop_update_timing_info();

        /* Run inner loop again with temperature = 0 so as to accept only swaps
         * which reduce the cost of the placement */
        annealer_->placement_inner_loop();

        if (placer_opts_.place_quench_algorithm.is_timing_driven()) {
            critical_path_ = timing_info_->least_slack_critical_path();
        }

        log_printer_.print_place_status(temperature_timer.elapsed_sec());
    }
    post_quench_timing_stats_ = timing_ctx.stats;

    // Final timing analysis
    const t_annealing_state& annealing_state = annealer_->get_annealing_state();
    PlaceCritParams crit_params;
    crit_params.crit_exponent = annealing_state.crit_exponent;
    crit_params.crit_limit = placer_opts_.place_crit_limit;
    crit_params.prob_strategy = placer_opts_.prob_timing_strategy;
    crit_params.prob_mc_samples = placer_opts_.prob_timing_mc_samples;
    crit_params.prob_risk_z = placer_opts_.prob_timing_risk_z;
    crit_params.prob_alpha = placer_opts_.prob_timing_alpha;
    crit_params.prob_beta = placer_opts_.prob_timing_beta;
    crit_params.prob_self_calibrate = placer_opts_.prob_self_calibrate;
    crit_params.prob_congestion_gamma = placer_opts_.prob_congestion_gamma;
    crit_params.prob_schedule_ramp = placer_opts_.prob_schedule_ramp;
    crit_params.prob_dist_func = placer_opts_.prob_dist_func;
    crit_params.prob_dist_threshold = placer_opts_.prob_dist_threshold;
    crit_params.prob_huber_delta = placer_opts_.prob_huber_delta;
    crit_params.prob_slack_gate = placer_opts_.prob_slack_gate;
    crit_params.prob_census_threshold = placer_opts_.prob_census_threshold;
    crit_params.prob_entropy_sharpening = placer_opts_.prob_entropy_sharpening;
    crit_params.prob_hallucination_dampen = placer_opts_.prob_hallucination_dampen;
    crit_params.prob_momentum_boost = placer_opts_.prob_momentum_boost;
    crit_params.net_cost_handler = &net_cost_handler_;
    crit_params.current_temp = annealing_state.t;

    if (placer_opts_.place_algorithm.is_timing_driven()) {
        perform_full_timing_update(placer_opts_,
                                   crit_params,
                                   place_delay_model_.get(),
                                   placer_criticalities_.get(),
                                   placer_setup_slacks_.get(),
                                   pin_timing_invalidator_.get(),
                                   timing_info_.get(),
                                   &costs_,
                                   placer_state_);

        critical_path_ = timing_info_->least_slack_critical_path();

        VTR_LOGV(!log_printer_.quiet(),
                 "post-quench CPD = %g (ns) \n", 1e9 * critical_path_.delay());
    }

    // See if our latest checkpoint is better than the current placement solution
    if (placer_opts_.place_checkpointing) {
        restore_best_placement(placer_opts_,
                               placer_state_,
                               placement_checkpoint_, timing_info_, costs_,
                               placer_criticalities_, placer_setup_slacks_, place_delay_model_,
                               pin_timing_invalidator_, crit_params, noc_cost_handler_);
    }

    if (placer_opts_.placement_saves_per_temperature >= 1) {
        std::string filename = vtr::string_fmt("placement_%03d_%03d.place",
                                               annealing_state.num_temps + 1, 0);
        VTR_LOGV(!log_printer_.quiet(), "Saving final placement to file: %s\n", filename.c_str());
        print_place(nullptr, nullptr, filename.c_str(), placer_state_.mutable_block_locs());
    }

    // Update physical pin values
    for (const ClusterBlockId block_id : cluster_ctx.clb_nlist.blocks()) {
        placer_state_.mutable_blk_loc_registry().place_sync_external_block_connections(block_id);
    }

    check_place_();

    log_printer_.print_post_placement_stats();
}

void Placer::update_global_state() {
    auto& mutable_palce_ctx = g_vpr_ctx.mutable_placement();

    // the placement location variables should be unlocked before being accessed
    mutable_palce_ctx.unlock_loc_vars();

    // copy the local location variables into the global state
    auto& global_blk_loc_registry = mutable_palce_ctx.mutable_blk_loc_registry();
    global_blk_loc_registry = placer_state_.blk_loc_registry();

#ifndef NO_GRAPHICS
    // update the graphics' reference to placement location variables
    get_draw_state_vars()->set_graphics_blk_loc_registry_ref(global_blk_loc_registry);
#endif
}
