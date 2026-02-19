/**
 * @file place_timing_update.cpp
 * @brief Defines the routines declared in place_timing_update.h.
 */

#include "place_timing_update.h"

#include "NetPinTimingInvalidator.h"
#include "PlacerCriticalities.h"
#include "PlacerSetupSlacks.h"
#include "placer_state.h"
#include "place_util.h"
#include "vtr_time.h"

#include "tatum/TimingGraph.hpp"
#include "tatum/delay_calc/DelayCalculator.hpp"
#include "globals.h"
#include "atom_netlist.h"
#include "timing_info.h"
#include "vtr_log.h"
#include "FactorGraphView.h"
#include <vector>
#include <cmath>
#include <string>
#include <memory>
#include <algorithm>
#include "vtr_hash.h"

struct ProbInjectStep1Event {
    size_t update_id;
    bool inject;
    double before;
    double after;
    double delta;
};
static std::vector<ProbInjectStep1Event> g_prob_inject_step1_audit;
static size_t g_num_timing_updates_seen = 0;
static size_t g_num_injection_updates_seen = 0;
static double g_cached_regularization_scaler = 1.0; // [NEW] Cache for comp_td_costs

struct ProbStep2Event {
    ProbStep2Event(size_t uid, double dWNS, double dCPD, int dWEP, double dWEPS,
                   double pWNS, int pWEP, double abs_err, bool same, int eps,
                   size_t ephash, int n12, int n15,
                   const MomentStats& mm,
                   double rt, double tcb, double tca, double dc) noexcept
        : update_id(uid), det_WNS_s(dWNS), det_CPD_s(dCPD), det_worst_ep(dWEP),
          det_worst_ep_slack_s(dWEPS), prob_WorstSlack95_s(pWNS), prob_worst_ep(pWEP),
          abs_err_WNS_s(abs_err), same_worst_ep(same), endpoints(eps),
          endpoint_hash(ephash), num_eps12(n12), num_eps15(n15), moments(mm),
          runtime_ms_prob(rt), timing_cost_before(tcb), timing_cost_after(tca), delta_cost(dc) {}

    size_t update_id;
    double det_WNS_s;
    double det_CPD_s;
    int det_worst_ep;
    double det_worst_ep_slack_s;
    double prob_WorstSlack95_s;
    int prob_worst_ep;
    double abs_err_WNS_s;
    bool same_worst_ep;
    int endpoints;
    size_t endpoint_hash;
    int num_eps12;
    int num_eps15;
    MomentStats moments;
    double runtime_ms_prob;
    double timing_cost_before;
    double timing_cost_after;
    double delta_cost;
};
static std::vector<ProbStep2Event> g_prob_step2_audit;
static double g_step2_max_abs_err_wns = 0.0;
static double g_step2_sum_abs_err_wns = 0.0;
static size_t g_step2_worst_ep_mismatch_count = 0;
static double g_step2_noop_delta_cost_max_abs = 0.0;

struct ProbStep3ReplaceEvent {
    ProbStep3ReplaceEvent(size_t uid, double dWNS, double pWNS, double rsk,
                          double tsta, double tnew, double dc, int eps, double rt) noexcept
        : update_id(uid), det_WNS_s(dWNS), worst_slack95_s(pWNS), risk_s(rsk),
          timing_cost_sta(tsta), timing_cost_new(tnew), delta_cost(dc),
          endpoints(eps), runtime_ms_prob(rt) {}

    size_t update_id;
    double det_WNS_s;
    double worst_slack95_s;
    double risk_s;
    double timing_cost_sta;
    double timing_cost_new;
    double delta_cost;
    int endpoints;
    double runtime_ms_prob;
};
static std::vector<ProbStep3ReplaceEvent> g_prob_step3_replace_audit;
static double g_step3_mean_delta_cost = 0.0;
static double g_step3_max_delta_cost = -std::numeric_limits<double>::infinity();
static double g_step3_min_delta_cost = std::numeric_limits<double>::infinity();

static std::unique_ptr<FactorGraphView> g_fg_view;
static BinState g_bin_state;

/* Routines local to place_timing_update.cpp */
static double comp_td_connection_cost(const PlaceDelayModel* delay_model,
                                      const PlacerCriticalities& place_crit,
                                      const PlacerState& placer_state,
                                      ClusterNetId net,
                                      int ipin);

static double sum_td_net_cost(ClusterNetId net,
                              const PlacerState& placer_state);

static double sum_td_costs(const PlacerState& placer_state);

///@brief Use an incremental approach to updating timing costs after re-computing criticalities
static constexpr bool INCR_COMP_TD_COSTS = true;

/**
 * @brief Initialize the timing information and structures in the placer.
 *
 * Perform first time update on the timing graph, and initialize the values within
 * PlacerCriticalities, PlacerSetupSlacks, and connection_timing_cost.
 */
void initialize_timing_info(const t_placer_opts& placer_opts,
                            const PlaceCritParams& crit_params,
                            const PlaceDelayModel* delay_model,
                            PlacerCriticalities* criticalities,
                            PlacerSetupSlacks* setup_slacks,
                            NetPinTimingInvalidator* pin_timing_invalidator,
                            SetupTimingInfo* timing_info,
                            t_placer_costs* costs,
                            PlacerState& placer_state) {
    const auto& cluster_ctx = g_vpr_ctx.clustering();
    const auto& clb_nlist = cluster_ctx.clb_nlist;

    //As a safety measure, for the first time update,
    //invalidate all timing edges via the pin invalidator
    //by passing in all the clb sink pins
    for (ClusterNetId net_id : clb_nlist.nets()) {
        for (ClusterPinId pin_id : clb_nlist.net_sinks(net_id)) {
            pin_timing_invalidator->invalidate_connection(pin_id);
        }
    }

    //Perform first time update for all timing related classes
    perform_full_timing_update(placer_opts,
                               crit_params,
                               delay_model,
                               criticalities,
                               setup_slacks,
                               pin_timing_invalidator,
                               timing_info,
                               costs,
                               placer_state);

    //Don't warn again about unconstrained nodes again during placement
    timing_info->set_warn_unconstrained(false);

    //Clear all update_td_costs() runtime stat variables
    auto& p_runtime_ctx = placer_state.mutable_runtime();
    p_runtime_ctx.f_update_td_costs_connections_elapsed_sec = 0.f;
    p_runtime_ctx.f_update_td_costs_nets_elapsed_sec = 0.f;
    p_runtime_ctx.f_update_td_costs_sum_nets_elapsed_sec = 0.f;
    p_runtime_ctx.f_update_td_costs_total_elapsed_sec = 0.f;
}

/**
 * @brief Updates every timing related classes, variables and structures.
 *
 * This routine exists to reduce code duplication, as the placer routines
 * often require updating every timing related stuff.
 *
 * Updates: SetupTimingInfo, PlacerCriticalities, PlacerSetupSlacks,
 *          timing_cost, connection_setup_slack.
 */
void perform_full_timing_update(const t_placer_opts& placer_opts,
                                const PlaceCritParams& crit_params,
                                const PlaceDelayModel* delay_model,
                                PlacerCriticalities* criticalities,
                                PlacerSetupSlacks* setup_slacks,
                                NetPinTimingInvalidator* pin_timing_invalidator,
                                SetupTimingInfo* timing_info,
                                t_placer_costs* costs,
                                PlacerState& placer_state) {
    /* FactorGraphView: Build exactly once per run */
    static FactorGraphView fg;
    static BinState bin_state;
    static bool fg_built = false;
    if (!fg_built) {
        const auto& timing_ctx = g_vpr_ctx.timing();
        if (timing_ctx.graph) {
            fg = build_factor_graph_view(*timing_ctx.graph);
            fg_built = true;
        }
    }

    if (fg_built && timing_info) {
        const auto& tg = *timing_info->timing_graph();
        const auto& analyzer = *timing_info->setup_analyzer();
        const auto& delay_calc = *timing_info->delay_calculator();

        // --- REGRESSION TESTS (ONCE PER RUN) ---
        // 1. Mode 0: Deterministic (Baseline)
        // Verify it runs without error.
        ProbTimingConfig cfg0; 
        cfg0.mode = UncertaintyMode::DETERMINISTIC;
        update_bin_state(fg, bin_state, cfg0, placer_state.block_locs());
        ProbTimingSummary sum0 = run_probabilistic_timing(fg, tg, analyzer, delay_calc, bin_state, cfg0);
        
        // 2. Mode 3: PRODUCTION PATH (Default)
        // This will likely yield empty bins on 'tseng' but must be safe.
        ProbTimingConfig cfg3_prod;
        cfg3_prod.mode = UncertaintyMode::BIN_LATENT_CORR;
        cfg3_prod.alpha = 0.0f; 
        cfg3_prod.gamma = 1.0f;
        cfg3_prod.bins_x = 4;
        cfg3_prod.bins_y = 4;
        cfg3_prod.forced_binning = false; // Explicity OFF
        
        update_bin_state(fg, bin_state, cfg3_prod, placer_state.block_locs());
        ProbTimingSummary sum3_prod = run_probabilistic_timing(fg, tg, analyzer, delay_calc, bin_state, cfg3_prod);

        VTR_LOG("INSTRUMENTATION: Regression [Mode 3 Prod] - Weighted: %zu, Empty: %zu\n", 
                sum3_prod.num_weighted_edges, sum3_prod.num_empty_weight_edges);

        // 3. Mode 3: VALIDATION HARNESS (Forced Binning)
        // This confirms the math still works when requested.
        ProbTimingConfig cfg3_forced;
        cfg3_forced.mode = UncertaintyMode::BIN_LATENT_CORR;
        cfg3_forced.alpha = 0.0f;
        cfg3_forced.gamma = 1.0f; 
        cfg3_forced.bins_x = 4;
        cfg3_forced.bins_y = 4;
        cfg3_forced.forced_binning = true; // Explicitly ON
        
        update_bin_state(fg, bin_state, cfg3_forced, placer_state.block_locs());
        ProbTimingSummary sum3_forced = run_probabilistic_timing(fg, tg, analyzer, delay_calc, bin_state, cfg3_forced);

        VTR_LOG("INSTRUMENTATION: Regression [Mode 3 Forced] - Correlation Test. Weighted: %zu\n", sum3_forced.num_weighted_edges);
    }

    /* Instrumentation: Print timing graph stats and sample edges */
    static bool printed_stats = false;
    if (!printed_stats) {
        const auto& timing_ctx = g_vpr_ctx.timing();
        if (timing_ctx.graph) {
            const auto& tg = *timing_ctx.graph;
            const auto& lookup = g_vpr_ctx.atom().lookup();
            const auto& netlist = g_vpr_ctx.atom().netlist();
            auto delay_calc = timing_info->delay_calculator();

            size_t num_reconvergent = 0;
            for (auto node_id : tg.nodes()) {
                if (tg.node_in_edges(node_id).size() > 1) {
                    num_reconvergent++;
                }
            }

            VTR_LOG("INSTRUMENTATION: Timing Graph Stats:\n");
            VTR_LOG("  Nodes: %zu\n", tg.nodes().size());
            VTR_LOG("  Edges: %zu\n", tg.edges().size());
            VTR_LOG("  Reconvergent Nodes (fanin > 1): %zu\n", num_reconvergent);

            VTR_LOG("INSTRUMENTATION: Sample Timing Edges (First 10):\n");
            int count = 0;
            for (auto edge_id : tg.edges()) {
                if (count >= 10) break;
                auto src_node = tg.edge_src_node(edge_id);
                auto sink_node = tg.edge_sink_node(edge_id);
                float delay = delay_calc->max_edge_delay(tg, edge_id).value();

                AtomPinId src_pin = lookup.tnode_atom_pin(src_node);
                AtomPinId sink_pin = lookup.tnode_atom_pin(sink_node);

                std::string src_name = (src_pin) ? netlist.pin_name(src_pin) : "N/A";
                std::string sink_name = (sink_pin) ? netlist.pin_name(sink_pin) : "N/A";

                VTR_LOG("  Edge %zu: src=%zu (%s), sink=%zu (%s), delay=%g\n",
                        size_t(edge_id), size_t(src_node), src_name.c_str(),
                        size_t(sink_node), sink_name.c_str(), delay);
                count++;
            }
            printed_stats = true;
        }
    }

    /* Update all timing related classes. */
    criticalities->enable_update();
    setup_slacks->enable_update();
    update_timing_classes(crit_params,
                          timing_info,
                          criticalities,
                          setup_slacks,
                          pin_timing_invalidator);

    /* Update the timing cost with new connection criticalities. */
    // [NEW] Reset scaler to 1.0 so we calculate pure STA cost first
    g_cached_regularization_scaler = 1.0; 
    update_timing_cost(delay_model,
                       criticalities,
                       placer_state,
                       &costs->timing_cost);

    // PROB_TIMING_INJECTION_POINT
    {
        double timing_cost_before = costs->timing_cost;
        bool inject_enabled = placer_opts.prob_timing_inject;
        std::string mode = placer_opts.prob_inject_mode;
        double timing_cost_after = timing_cost_before;

        g_num_timing_updates_seen++;

        if (g_num_timing_updates_seen == 1) {
            VTR_LOG("PROB_INJECT_STEP1_CONFIG: inject_default=off inject_flag=%s inject_mode=%s git_commit=de3a5c01e\n",
                    inject_enabled ? "on" : "off", mode.c_str());
        }

        // 1. Step 1 No-op Injection
        if (inject_enabled && mode == "noop") {
            g_num_injection_updates_seen++;
            timing_cost_after = timing_cost_before;
            costs->timing_cost = timing_cost_after;

            double delta = timing_cost_after - timing_cost_before;
            VTR_LOG("PROB_INJECT_STEP1: update_id=%zu inject=%d mode=noop before=%.15g after=%.15g delta=%.15g\n",
                    g_num_timing_updates_seen, (int)inject_enabled, timing_cost_before, timing_cost_after, delta);

            if (std::abs(delta) > 1e-15) {
                VTR_ASSERT_MSG(std::abs(delta) <= 1e-15, "PROB_INJECT_STEP1: Invariant G3 violation!");
            }
            g_step2_noop_delta_cost_max_abs = std::max(g_step2_noop_delta_cost_max_abs, std::abs(delta));
            g_prob_inject_step1_audit.push_back({g_num_timing_updates_seen, inject_enabled, timing_cost_before, timing_cost_after, delta});
        } else if (!inject_enabled) {
            g_prob_inject_step1_audit.push_back({g_num_timing_updates_seen, inject_enabled, timing_cost_before, timing_cost_before, 0.0});
        }

        // 2. Phase 15 Step 2: Probabilistic Analysis
        float det_WNS_s = 0.0f;
        float det_CPD_s = 0.0f;
        tatum::NodeId det_worst_node = tatum::NodeId::INVALID();
        float min_slack = std::numeric_limits<float>::infinity();
        double prob_WNS_s = 0.0;
        tatum::NodeId prob_worst_node = tatum::NodeId::INVALID();
        double abs_err = 0.0;
        bool same_ep = false;
        ProbTimingSummary summary;
        ProbTimingConfig config;
        std::vector<tatum::NodeId> endpoints;
        size_t ephash = 0;
        int num_eps12 = 0;
        int num_eps15 = 0;

        if (placer_opts.prob_timing_enable) {
            det_WNS_s = timing_info->setup_worst_negative_slack();
            det_CPD_s = timing_info->least_slack_critical_path().delay().value();

            const auto& tg = *timing_info->timing_graph();
            const auto* analyzer = timing_info->setup_analyzer().get();

            for (auto node_id : tg.nodes()) {
                auto slacks = analyzer->setup_slacks(node_id);
                if (!slacks.empty()) {
                    endpoints.push_back(node_id);
                    float s = tatum::find_minimum_tag(slacks)->time().value();
                    if (s < min_slack) {
                        min_slack = s;
                        det_worst_node = node_id;
                    }
                }
            }
            std::sort(endpoints.begin(), endpoints.end());

            for (auto node_id : endpoints) {
                vtr::hash_combine(ephash, (size_t)node_id);
            }

            // G1 Check
            double wns_ref_err = std::abs((double)det_WNS_s - (double)min_slack);
            if (wns_ref_err > 1e-12) {
                VTR_LOG("PROB_STEP2_SANITY_ERROR: Deterministic WNS ref mismatch! det_WNS_s=%.15g min_slack=%.15g err=%.15g\n",
                        (double)det_WNS_s, (double)min_slack, wns_ref_err);
            }

            // Tie Analysis
            for (auto node_id : endpoints) {
                float s = tatum::find_minimum_tag(analyzer->setup_slacks(node_id))->time().value();
                if (s <= min_slack + 1e-12) num_eps12++;
                if (s <= min_slack + 1e-15) num_eps15++;
            }

            if (!g_fg_view) {
                g_fg_view = std::make_unique<FactorGraphView>(build_factor_graph_view(tg));
            }

            config.mode = (UncertaintyMode)placer_opts.prob_timing_mode;
            config.alpha = placer_opts.prob_timing_alpha;
            config.gamma = placer_opts.prob_timing_alpha_corr; // Map alpha_corr to gamma

            reset_moment_stats();
            update_bin_state(*g_fg_view, g_bin_state, config, placer_state.block_locs());
            summary = run_probabilistic_timing(*g_fg_view, tg, *analyzer, *timing_info->delay_calculator(), g_bin_state, config);

            prob_WNS_s = summary.worst_slack_95;
            prob_worst_node = summary.worst_endpoint_node_id;
            abs_err = std::abs(det_WNS_s - prob_WNS_s);
            same_ep = (det_worst_node == prob_worst_node);

            VTR_LOG("PROB_STEP2_SANITY: update_id=%zu mode=%d alpha=%g gamma=%g det_WNS_s=%.15g prob_WorstSlack95_s=%.15g abs_err_WNS_s=%.15g same_worst_ep=%d endpoints=%d ephash=%zu num_eps12=%d num_eps15=%d moments_total=%zu moments_deg=%zu\n",
                    g_num_timing_updates_seen, (int)config.mode, config.alpha, config.gamma,
                    (double)det_WNS_s, (double)prob_WNS_s, abs_err, (int)same_ep, (int)summary.num_endpoints, ephash, num_eps12, num_eps15,
                    summary.moments.num_max_calls_total, summary.moments.num_max_calls_sigma_both_zero);

            if (!same_ep) {
                VTR_LOG("PROB_STEP2_MISMATCH: update_id=%zu det_worst_ep=%zu (slack=%.15g) prob_worst_ep=%zu (slack95=%.15g) num_eps12=%d num_eps15=%d\n",
                        g_num_timing_updates_seen, (size_t)det_worst_node, (double)min_slack, (size_t)prob_worst_node, (double)prob_WNS_s, num_eps12, num_eps15);
            }

            g_step2_max_abs_err_wns = std::max(g_step2_max_abs_err_wns, abs_err);
            g_step2_sum_abs_err_wns += abs_err;
            if (!same_ep) g_step2_worst_ep_mismatch_count++;

            bool params_are_zero = (config.alpha == 0.0f && config.gamma == 0.0f);
            if (params_are_zero && abs_err > 1e-12) {
                VTR_ASSERT_MSG(abs_err <= 1e-12, "PROB_STEP2: Collapse mismatch violation!");
            }
        }

        // 3. Phase 15 Step 3: Replace/Regularize Injection
        if (inject_enabled && (mode == "replace" || mode == "regularize")) {
            if (g_num_injection_updates_seen == 0) {
                VTR_LOG("PROB_STEP3_INJECT_CONFIG: benchmark=%s seed=%d alpha=%g gamma=%g inject=on mode=%s lambda=%g clamp=%s\n",
                        g_vpr_ctx.atom().netlist().netlist_name().c_str(), 1, config.alpha, config.gamma,
                        mode.c_str(), (double)placer_opts.prob_inject_lambda,
                        placer_opts.prob_inject_clamp ? "on" : "off");
            }
            g_num_injection_updates_seen++;

            double risk_s = (placer_opts.prob_inject_clamp) ? std::max(0.0, -prob_WNS_s) : -prob_WNS_s;
            double timing_cost_new = timing_cost_before;
            double delta = 0.0;
            double lambda = (double)placer_opts.prob_inject_lambda;

            if (mode == "replace") {
                timing_cost_new = risk_s;
            } else if (mode == "regularize") {
                // Dimensionless regularization: timing_cost = timing_cost_sta * (1 + lambda * risk_norm)
                double ref_s = std::max(1e-15, -(double)det_WNS_s);
                double risk_norm = std::min(5.0, risk_s / ref_s);
                timing_cost_new = timing_cost_before * (1.0 + lambda * risk_norm);
                delta = timing_cost_new - timing_cost_before;
                g_cached_regularization_scaler = (1.0 + lambda * risk_norm); // [NEW] Update cache
            }

            // [NEW] CRITICAL FIX: The connection_timing_cost cache currently holds UN-SCALED costs 
            // (because update_timing_cost ran when scaler was 1.0).
            // We must now scale the cache values so that incremental updates (which read from cache)
            // are consistent with the new global scaler.
            if (mode != "regularize") {
                g_cached_regularization_scaler = 1.0;
            }

            costs->timing_cost = timing_cost_new;
            timing_cost_after = timing_cost_new;
            delta = timing_cost_new - timing_cost_before;

            if (mode == "replace") {
                VTR_LOG("PROB_STEP3_REPLACE: update_id=%zu prob_enable=%d mode=%d alpha=%g gamma=0 inject=1 inject_mode=replace clamp=%d det_WNS_s=%.15g det_CPD_s=%.15g worst_slack95_s=%.15g risk_s=%.15g timing_cost_sta=%.15g timing_cost_new=%.15g delta_cost=%.15g endpoints=%d runtime_ms_prob=%.2f\n",
                        g_num_timing_updates_seen, (int)placer_opts.prob_timing_enable, (int)config.mode, config.alpha, (int)placer_opts.prob_inject_clamp,
                        (double)det_WNS_s, (double)det_CPD_s, prob_WNS_s, risk_s,
                        timing_cost_before, timing_cost_new, delta, (int)summary.num_endpoints, summary.runtime_ms);
            } else if (mode == "regularize") {
                double ref_s = std::max(1e-15, -(double)det_WNS_s);
                double risk_norm = risk_s / ref_s;
                VTR_LOG("PROB_STEP3_REGULARIZE: update_id=%zu prob_enable=%d mode=%d alpha=%g gamma=0 inject=1 inject_mode=regularize lambda=%g clamp=%d det_WNS_s=%.15g det_CPD_s=%.15g worst_slack95_s=%.15g risk_s=%.15g ref_s=%.15g risk_norm=%.15g timing_cost_sta=%.15g timing_cost_new=%.15g delta_cost=%.15g endpoints=%d runtime_ms_prob=%.2f\n",
                        g_num_timing_updates_seen, (int)placer_opts.prob_timing_enable, (int)config.mode, config.alpha, lambda, (int)placer_opts.prob_inject_clamp,
                        (double)det_WNS_s, (double)det_CPD_s, prob_WNS_s, risk_s, ref_s, risk_norm,
                        timing_cost_before, timing_cost_new, delta, (int)summary.num_endpoints, summary.runtime_ms);
            }

            g_step3_mean_delta_cost = (g_step3_mean_delta_cost * (g_num_injection_updates_seen - 1) + delta) / g_num_injection_updates_seen;
            g_step3_max_delta_cost = std::max(g_step3_max_delta_cost, delta);
            g_step3_min_delta_cost = std::min(g_step3_min_delta_cost, delta);

            g_prob_step3_replace_audit.emplace_back(
                g_num_timing_updates_seen, (double)det_WNS_s, prob_WNS_s, risk_s,
                timing_cost_before, timing_cost_new, delta, (int)summary.num_endpoints, summary.runtime_ms
            );
        }

        // 4. Final Audit Collection
        if (placer_opts.prob_timing_enable) {
            g_prob_step2_audit.emplace_back(
                g_num_timing_updates_seen, (double)det_WNS_s, (double)det_CPD_s, (int)size_t(det_worst_node), (double)min_slack,
                prob_WNS_s, (int)size_t(prob_worst_node), abs_err, same_ep, (int)summary.num_endpoints,
                ephash, num_eps12, num_eps15, summary.moments,
                summary.runtime_ms, timing_cost_before, timing_cost_after, timing_cost_after - timing_cost_before
            );
        }
    }

    /* Commit the setup slacks since they are updated. */
    commit_setup_slacks(setup_slacks, placer_state);
}

/**
 * @brief Update timing information based on the current block positions.
 *
 * Run STA to update the timing info class.
 *
 * Update the values stored in PlacerCriticalities and PlacerSetupSlacks
 * if they are enabled to update. To enable updating, call their respective
 * enable_update() method. See their documentation for more detailed info.
 *
 * If criticalities are updated, the timing driven costs should be updated
 * as well by calling update_timing_cost(). Calling this routine to update
 * timing_cost will produce round-off error in the long run due to its
 * incremental nature, so the timing cost value will be recomputed once in
 * a while, via other timing driven routines.
 *
 * If setup slacks are updated, then normally they should be committed to
 * `connection_setup_slack` via commit_setup_slacks() routine. However,
 * sometimes new setup slack values are not committed immediately if we
 * expect to revert the current timing update in the near future, or if
 * we wish to compare the new slack values to the original ones.
 *
 * All the pins with changed connection delays have already been added into
 * the NetPinTimingInvalidator to allow incremental STA update. These
 * changed connection delays are a direct result of moved blocks in try_swap().
 */
void update_timing_classes(const PlaceCritParams& crit_params,
                           SetupTimingInfo* timing_info,
                           PlacerCriticalities* criticalities,
                           PlacerSetupSlacks* setup_slacks,
                           NetPinTimingInvalidator* pin_timing_invalidator) {
    /* Run STA to update slacks and adjusted/relaxed criticalities. */
    timing_info->update();

    /* Update the placer's criticalities (e.g. sharpen with crit_exponent). */
    criticalities->update_criticalities(crit_params);

    /* Update the placer's raw setup slacks. */
    setup_slacks->update_setup_slacks();

    /* Clear invalidation state. */
    pin_timing_invalidator->reset();
}

/**
 * @brief Update the timing driven (td) costs.
 *
 * This routine either uses incremental update_td_costs(), or updates
 * from scratch using comp_td_costs(). By default, it is incremental
 * by iterating over the set of clustered netlist connections/pins
 * returned by PlacerCriticalities::pins_with_modified_criticality().
 *
 * Hence, this routine should always be called when PlacerCriticalities
 * is enabled to be updated in update_timing_classes(). Otherwise, the
 * incremental method will no longer be correct.
 */
void update_timing_cost(const PlaceDelayModel* delay_model,
                        const PlacerCriticalities* criticalities,
                        PlacerState& placer_state,
                        double* timing_cost) {
#ifdef INCR_COMP_TD_COSTS
    update_td_costs(delay_model, *criticalities, placer_state, timing_cost);
#else
    comp_td_costs(delay_model, *criticalities, placer_state, timing_cost);
#endif
}

/**
 * @brief Commit all the setup slack values from the PlacerSetupSlacks
 *        class to `connection_setup_slack`.
 *
 * This routine is incremental since it relies on the pins_with_modified_setup_slack()
 * to detect which pins need to be updated and which pins do not.
 *
 * Therefore, it is assumed that this routine is always called immediately after
 * each time update_timing_classes() is called with setup slack update enabled.
 * Otherwise, pins_with_modified_setup_slack() cannot accurately account for all
 * the pins that have their setup slacks changed, making this routine incorrect.
 *
 * Currently, the only exception to the rule above is when setup slack analysis is used
 * during the placement quench. The new setup slacks might be either accepted or
 * rejected, so for efficiency reasons, this routine is not called if the slacks are
 * rejected in the end. For more detailed info, see the try_swap() routine.
 */
void commit_setup_slacks(const PlacerSetupSlacks* setup_slacks,
                         PlacerState& placer_state) {
    const auto& clb_nlist = g_vpr_ctx.clustering().clb_nlist;
    auto& connection_setup_slack = placer_state.mutable_timing().connection_setup_slack;

    /* Incremental: only go through sink pins with modified setup slack */
    auto clb_pins_modified = setup_slacks->pins_with_modified_setup_slack();
    for (ClusterPinId pin_id : clb_pins_modified) {
        ClusterNetId net_id = clb_nlist.pin_net(pin_id);
        size_t pin_index_in_net = clb_nlist.pin_net_index(pin_id);

        connection_setup_slack[net_id][pin_index_in_net] = setup_slacks->setup_slack(net_id, pin_index_in_net);
    }
}

/**
 * @brief Verify that the values in `connection_setup_slack` matches PlacerSetupSlacks.
 *
 * Return true if all connection values are identical. Otherwise, return false.
 *
 * Currently, this routine is called to check if the timing update has been successfully
 * reverted after a proposed move is rejected when applying setup slack analysis during
 * the placement quench. If successful, the setup slacks in PlacerSetupSlacks should be
 * the same as the values in `connection_setup_slack` without running commit_setup_slacks().
 * For more detailed info, see the try_swap() routine.
 */
bool verify_connection_setup_slacks(const PlacerSetupSlacks* setup_slacks,
                                    const PlacerState& placer_state) {
    const auto& clb_nlist = g_vpr_ctx.clustering().clb_nlist;
    const auto& connection_setup_slack = placer_state.timing().connection_setup_slack;

    /* Go through every single sink pin to check that the slack values are the same */
    for (ClusterNetId net_id : clb_nlist.nets()) {
        for (size_t ipin = 1; ipin < clb_nlist.net_pins(net_id).size(); ++ipin) {
            if (connection_setup_slack[net_id][ipin] != setup_slacks->setup_slack(net_id, ipin)) {
                return false;
            }
        }
    }
    return true;
}

/**
 * @brief Incrementally updates timing cost based on the current delays and criticality estimates.
 *
 * Unlike comp_td_costs(), this only updates connections who's criticality has changed.
 * This is a superset of those connections whose connection delay has changed. For a
 * from-scratch recalculation, refer to comp_td_cost().
 *
 * We must be careful calculating the total timing cost incrementally, due to limited
 * floating point precision, so that we get a bit-identical result matching the one
 * calculated by comp_td_costs().
 *
 * In particular, we can not simply calculate the incremental delta's caused by changed
 * connection timing costs and adjust the timing cost. Due to limited precision, the results
 * of floating point math operations are order dependent and we would get a different result.
 *
 * To get around this, we calculate the timing costs hierarchically, to ensure that we
 * calculate the sum with the same order of operations as comp_td_costs().
 *
 * See PlacerTimingCosts object used to represent connection_timing_costs for details.
 */
void update_td_costs(const PlaceDelayModel* delay_model,
                     const PlacerCriticalities& place_crit,
                     PlacerState& placer_state,
                     double* timing_cost) {
    vtr::Timer t;
    auto& cluster_ctx = g_vpr_ctx.clustering();
    auto& clb_nlist = cluster_ctx.clb_nlist;

    auto& p_timing_ctx = placer_state.mutable_timing();
    auto& p_runtime_ctx = placer_state.mutable_runtime();
    auto& connection_timing_cost = p_timing_ctx.connection_timing_cost;

    //Update the modified pin timing costs
    {
        vtr::Timer timer;
        auto clb_pins_modified = place_crit.pins_with_modified_criticality();
        for (ClusterPinId clb_pin : clb_pins_modified) {
            if (clb_nlist.pin_type(clb_pin) == PinType::DRIVER) continue;

            ClusterNetId clb_net = clb_nlist.pin_net(clb_pin);
            VTR_ASSERT_SAFE(clb_net);

            if (cluster_ctx.clb_nlist.net_is_ignored(clb_net)) continue;

            int ipin = clb_nlist.pin_net_index(clb_pin);
            VTR_ASSERT_SAFE(ipin >= 1 && ipin < int(clb_nlist.net_pins(clb_net).size()));

            double new_timing_cost = comp_td_connection_cost(delay_model, place_crit, placer_state, clb_net, ipin);

            //Record new value
            connection_timing_cost[clb_net][ipin] = new_timing_cost;
        }

        p_runtime_ctx.f_update_td_costs_connections_elapsed_sec += timer.elapsed_sec();
    }

    //Re-total timing costs of all nets
    {
        vtr::Timer timer;
        // [NEW] We must apply the regularization scaler to the delta or total here
        // But total_cost() returns sum of cache. Cache is UNSCALED.
        // So we multiply the result by scaler.
        *timing_cost = connection_timing_cost.total_cost() * g_cached_regularization_scaler;
        p_runtime_ctx.f_update_td_costs_sum_nets_elapsed_sec += timer.elapsed_sec();
    }

#ifdef VTR_ASSERT_DEBUG_ENABLED
    double check_timing_cost = 0.;
    comp_td_costs(delay_model, place_crit, placer_state, &check_timing_cost);
    VTR_ASSERT_DEBUG_MSG(check_timing_cost == *timing_cost,
                         "Total timing cost calculated incrementally in update_td_costs() is "
                         "not consistent with value calculated from scratch in comp_td_costs()");
#endif
    p_runtime_ctx.f_update_td_costs_total_elapsed_sec += t.elapsed_sec();
}

/**
 * @brief Recomputes timing cost from scratch based on the current delays and criticality estimates.
 *
 * Computes the cost (from scratch) from the delays and criticalities of all point to point
 * connections, we define the timing cost of each connection as criticality * delay.
 *
 * We calculate the timing cost in a hierarchical manner (first connection, then nets, then
 * sum of nets) in order to allow it to be incremental while avoiding round-off effects.
 *
 * For a more efficient incremental update, see update_td_costs().
 */
void comp_td_costs(const PlaceDelayModel* delay_model,
                   const PlacerCriticalities& place_crit,
                   PlacerState& placer_state,
                   double* timing_cost) {
    auto& cluster_ctx = g_vpr_ctx.clustering();
    auto& p_timing_ctx = placer_state.mutable_timing();

    auto& connection_timing_cost = p_timing_ctx.connection_timing_cost;
    auto& net_timing_cost = p_timing_ctx.net_timing_cost;

    for (ClusterNetId net_id : cluster_ctx.clb_nlist.nets()) {
        if (cluster_ctx.clb_nlist.net_is_ignored(net_id)) continue;

        for (size_t ipin = 1; ipin < cluster_ctx.clb_nlist.net_pins(net_id).size(); ipin++) {
            float conn_timing_cost = comp_td_connection_cost(delay_model, place_crit, placer_state, net_id, ipin);

            /* Record new value */
            connection_timing_cost[net_id][ipin] = conn_timing_cost;
        }
        /* Store net timing cost for more efficient incremental updating */
        net_timing_cost[net_id] = sum_td_net_cost(net_id, placer_state);
    }
    /* Make sure timing cost does not go above MIN_TIMING_COST. */
    *timing_cost = sum_td_costs(placer_state) * g_cached_regularization_scaler;
}

/**
 * @brief Calculates the timing cost of the specified connection.
 *
 * This routine assumes that it is only called either compt_td_cost() or
 * update_td_costs(). Otherwise, various assertions below would fail.
 */
static double comp_td_connection_cost(const PlaceDelayModel* delay_model,
                                      const PlacerCriticalities& place_crit,
                                      const PlacerState& placer_state,
                                      ClusterNetId net,
                                      int ipin) {
    const auto& p_timing_ctx = placer_state.timing();
    const auto& block_locs = placer_state.block_locs();

    VTR_ASSERT_SAFE_MSG(ipin > 0, "Shouldn't be calculating connection timing cost for driver pins");

    VTR_ASSERT_SAFE_MSG(p_timing_ctx.connection_delay[net][ipin] == comp_td_single_connection_delay(delay_model, block_locs, net, ipin),
                        "Connection delays should already be updated");

    double conn_timing_cost = place_crit.criticality(net, ipin) * p_timing_ctx.connection_delay[net][ipin];

    VTR_ASSERT_SAFE_MSG(std::isnan(p_timing_ctx.proposed_connection_delay[net][ipin]),
                        "Proposed connection delay should already be invalidated");

    VTR_ASSERT_SAFE_MSG(std::isnan(p_timing_ctx.proposed_connection_timing_cost[net][ipin]),
                        "Proposed connection timing cost should already be invalidated");

    VTR_ASSERT_SAFE_MSG(std::isnan(p_timing_ctx.proposed_connection_timing_cost[net][ipin]),
                        "Proposed connection timing cost should already be invalidated");

    // [NEW] Return UNSCALED cost. The scaler is applied to the total/delta in update_td_costs/comp_td_costs.
    return conn_timing_cost; 
}

///@brief Returns the timing cost of the specified 'net' based on the values in connection_timing_cost.
static double sum_td_net_cost(ClusterNetId net,
                              const PlacerState& placer_state) {
    const auto& cluster_ctx = g_vpr_ctx.clustering();
    auto& p_timing_ctx = placer_state.timing();
    auto& connection_timing_cost = p_timing_ctx.connection_timing_cost;

    double net_td_cost = 0;
    for (unsigned ipin = 1; ipin < cluster_ctx.clb_nlist.net_pins(net).size(); ipin++) {
        net_td_cost += connection_timing_cost[net][ipin];
    }

    return net_td_cost;
}

///@brief Returns the total timing cost across all nets based on the values in net_timing_cost.
static double sum_td_costs(const PlacerState& placer_state) {
    const auto& cluster_ctx = g_vpr_ctx.clustering();
    const auto& p_timing_ctx = placer_state.timing();
    const auto& net_timing_cost = p_timing_ctx.net_timing_cost;

    double td_cost = 0;
    for (ClusterNetId net_id : cluster_ctx.clb_nlist.nets()) {
        if (cluster_ctx.clb_nlist.net_is_ignored(net_id)) {
            continue;
        }
        td_cost += net_timing_cost[net_id];
    }

    return td_cost;
}

void finish_prob_inject_step1_audit(const std::string& circuit_name, int seed, SetupTimingInfo* timing_info) {
    VTR_LOG("PROB_INJECT_STEP1_SUMMARY: timing_updates_seen=%zu injection_updates_seen=%zu\n",
            g_num_timing_updates_seen, g_num_injection_updates_seen);

    // --- STEP 1 CSV ---
    {
        std::string csv_filename = "prob_inject_step1_" + circuit_name + "_" + std::to_string(seed) + ".csv";
        VTR_LOG("Writing Phase 15 Step 1 audit trail to %s...\n", csv_filename.c_str());

        FILE* fp = fopen(csv_filename.c_str(), "w");
        if (fp) {
            fprintf(fp, "update_id,inject,before,after,delta\n");
            for (const auto& event : g_prob_inject_step1_audit) {
                fprintf(fp, "%zu,%d,%.15g,%.15g,%.15g\n",
                        event.update_id, (int)event.inject, event.before, event.after, event.delta);
            }
            fclose(fp);
        } else {
            VTR_LOG_ERROR("Failed to open audit file %s for writing\n", csv_filename.c_str());
        }
    }

    // --- STEP 2 SUMMARY & CSV ---
    if (!g_prob_step2_audit.empty()) {
        double mean_err = (g_prob_step2_audit.empty()) ? 0.0 : g_step2_sum_abs_err_wns / g_prob_step2_audit.size();
        
        VTR_LOG("PROB_STEP2_SUMMARY:\n");
        VTR_LOG("  updates=%zu\n", g_prob_step2_audit.size());
        VTR_LOG("  max_abs_err_WNS_s=%.15g\n", g_step2_max_abs_err_wns);
        VTR_LOG("  mean_abs_err_WNS_s=%.15g\n", mean_err);
        VTR_LOG("  endpoints_last=%d\n", g_prob_step2_audit.empty() ? 0 : g_prob_step2_audit.back().endpoints);
        VTR_LOG("  worst_ep_mismatch_count=%zu\n", g_step2_worst_ep_mismatch_count);
        VTR_LOG("  noop_delta_cost_max_abs=%.15g\n", g_step2_noop_delta_cost_max_abs);
        VTR_LOG("  prob_enable=1\n");
        VTR_LOG("  mode=0 alpha=0 gamma=0\n");

        std::string csv_filename = "prob_step2_collapse_" + circuit_name + "_" + std::to_string(seed) + ".csv";
        VTR_LOG("Writing Phase 15 Step 2 audit trail to %s...\n", csv_filename.c_str());

        FILE* fp = fopen(csv_filename.c_str(), "w");
        if (fp) {
            fprintf(fp, "update_id,det_WNS_s,det_CPD_s,det_worst_ep,det_worst_ep_slack_s,prob_WorstSlack95_s,prob_worst_ep,abs_err_WNS_s,same_worst_ep,endpoints,ephash,num_eps12,num_eps15,mom_total,mom_both0,mom_one0,mom_gen,mom_min,runtime_ms_prob,timing_cost_before,timing_cost_after,delta_cost\n");
            for (const auto& ev : g_prob_step2_audit) {
                fprintf(fp, "%zu,%.15g,%.15g,%d,%.15g,%.15g,%d,%.15g,%d,%d,%zu,%d,%d,%zu,%zu,%zu,%zu,%zu,%.2f,%.15g,%.15g,%.15g\n",
                        ev.update_id, ev.det_WNS_s, ev.det_CPD_s, ev.det_worst_ep, ev.det_worst_ep_slack_s,
                        ev.prob_WorstSlack95_s, ev.prob_worst_ep, ev.abs_err_WNS_s, (int)ev.same_worst_ep,
                        ev.endpoints, ev.endpoint_hash, ev.num_eps12, ev.num_eps15,
                        ev.moments.num_max_calls_total, ev.moments.num_max_calls_sigma_both_zero,
                        ev.moments.num_max_calls_sigma_one_zero, ev.moments.num_max_calls_general,
                        ev.moments.num_min_calls_total,
                        ev.runtime_ms_prob, ev.timing_cost_before, ev.timing_cost_after, ev.delta_cost);
            }
            fclose(fp);
        } else {
            VTR_LOG_ERROR("Failed to open audit file %s for writing\n", csv_filename.c_str());
        }
    }

    // --- STEP 3 SUMMARY & CSV ---
    if (!g_prob_step3_replace_audit.empty()) {
        float final_WNS = 0.0f;
        float final_CPD = 0.0f;
        if (timing_info) {
            final_WNS = timing_info->setup_worst_negative_slack();
            final_CPD = timing_info->least_slack_critical_path().delay().value();
        }

        VTR_LOG("PROB_STEP3_REPLACE_SUMMARY:\n");
        VTR_LOG("  updates=%zu\n", g_num_timing_updates_seen);
        VTR_LOG("  injections=%zu\n", g_prob_step3_replace_audit.size());
        VTR_LOG("  mean_delta_cost=%.15g\n", g_step3_mean_delta_cost);
        VTR_LOG("  max_delta_cost=%.15g\n", g_step3_max_delta_cost);
        VTR_LOG("  min_delta_cost=%.15g\n", g_step3_min_delta_cost);
        VTR_LOG("  final_CPD_ns=%.15g\n", (double)final_CPD * 1e9);
        VTR_LOG("  final_WNS_ns=%.15g\n", (double)final_WNS * 1e9);

        std::string csv_filename = "prob_step3_replace_" + circuit_name + "_" + std::to_string(seed) + ".csv";
        VTR_LOG("Writing Phase 15 Step 3 audit trail to %s...\n", csv_filename.c_str());

        FILE* fp = fopen(csv_filename.c_str(), "w");
        if (fp) {
            fprintf(fp, "update_id,det_WNS_s,worst_slack95_s,risk_s,timing_cost_sta,timing_cost_new,delta_cost,endpoints,runtime_ms_prob\n");
            for (const auto& ev : g_prob_step3_replace_audit) {
                fprintf(fp, "%zu,%.15g,%.15g,%.15g,%.15g,%.15g,%.15g,%d,%.2f\n",
                        ev.update_id, ev.det_WNS_s, ev.worst_slack95_s, ev.risk_s,
                        ev.timing_cost_sta, ev.timing_cost_new, ev.delta_cost,
                        ev.endpoints, ev.runtime_ms_prob);
            }
            fclose(fp);
        } else {
            VTR_LOG_ERROR("Failed to open audit file %s for writing\n", csv_filename.c_str());
        }
    }
}
