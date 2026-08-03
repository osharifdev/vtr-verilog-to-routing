/**
 * @file place_timing_update.cpp
 * @brief Defines the routines declared in place_timing_update.h.
 */

#include "place_timing_update.h"

#include <chrono>
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
#include "timing_util.h"
#include <vector>
#include <cmath>
#include <string>
#include <memory>
#include <algorithm>
#include "vtr_hash.h"
#include "net_cost_handler.h"

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
static double g_cached_regularization_scaler = 1.0; // [REMOVED] Replaced by connection-specific scaling
static double g_cached_risk_norm = 0.0;           // [NEW] Normalized risk for connection scaling
static double g_cached_prob_lambda = 0.0;         // [NEW] Lambda for connection scaling
static e_prob_dist_func g_cached_prob_dist_func = e_prob_dist_func::LINEAR;
static float g_cached_prob_dist_threshold = 0.0f;
static float g_cached_prob_huber_delta = 20.0f;
static ClbNetPinsMatrix<float> g_prob_differential_crit;
static vtr::vector<ClusterNetId, float> g_net_congestion_cache; // [OPTION C] Per-net congestion for uplift scaling
static float g_congestion_uplift_gamma = 0.0f; // [OPTION C] How strongly congestion reduces uplift (0=off)
double g_last_deterministic_cpd = -1.0;
double g_adaptive_momentum_scaler = 1.0;

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
static PhysicalState g_phys_state;

/* Routines local to place_timing_update.cpp */

static double sum_td_net_cost(ClusterNetId net,
                              const PlacerState& placer_state);

static double sum_td_costs(const PlacerState& placer_state);

/**
 * @brief [PHASE 7] Topological Resolver
 * Maps structural circuit metrics (Reconvergence Density R, Depth D) to optimal probabilistic parameters.
 */
struct TopologicalConfig {
    float alpha;
    float lambda;
};

static TopologicalConfig resolve_topological_config(const FactorGraphView& fg) {
    TopologicalConfig cfg;
    
    // Calibrated Heuristic: Alpha-Reconvergence Law (v2)
    // R (density) typically 0.1-0.3. Target alpha 0.10-0.15.
    cfg.alpha = 0.6f * fg.reconvergence_density + 0.02f;
    
    // Lambda (injection) scales with logic depth
    cfg.lambda = 0.001f * fg.median_logic_depth + 0.04f;
    cfg.lambda = std::min(cfg.lambda, 0.15f); // Cap to prevent divergence
    
    VTR_LOG("AUTONOMOUS_ENGINE: Resolved structure R=%.4f D=%.2f -> α=%.4f λ=%.4f\n", 
            fg.reconvergence_density, fg.median_logic_depth, cfg.alpha, cfg.lambda);
            
    return cfg;
}

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
    /* Ensure FactorGraphView is built and synchronized with current timing graph */
    if (timing_info) {
        const auto& tg = *timing_info->timing_graph();
        if (!g_fg_view || g_fg_view->num_nodes != tg.nodes().size()) {
            if (g_fg_view) {
                VTR_LOG("INSTRUMENTATION: Final Audit Timing Graph mismatch (%zu vs %zu). Rebuilding...\n", 
                        g_fg_view->num_nodes, tg.nodes().size());
            }
            g_fg_view = std::make_unique<FactorGraphView>(build_factor_graph_view(tg));
        }
    }
    FactorGraphView& fg = *g_fg_view;

    if (timing_info) {
        const auto& tg = *timing_info->timing_graph();
        const auto& analyzer = *timing_info->setup_analyzer();
        const auto& delay_calc = *timing_info->delay_calculator();

    // [REMOVED] Regression Tests for Synchronization Audit
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
    g_cached_risk_norm = 0.0;
    g_cached_prob_lambda = 0.0; 
    
    // [NEW] Update cached geometric parameters
    g_cached_prob_dist_func = placer_opts.prob_dist_func;
    g_cached_prob_dist_threshold = placer_opts.prob_dist_threshold;
    g_cached_prob_huber_delta = placer_opts.prob_huber_delta;

    comp_td_costs(delay_model,
                  *criticalities,
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

            if (!g_fg_view || g_fg_view->num_nodes != tg.nodes().size()) {
                if (g_fg_view) {
                    VTR_LOG("INSTRUMENTATION: Timing Graph size mismatch (%zu vs %zu). Rebuilding FactorGraphView...\n", 
                            g_fg_view->num_nodes, tg.nodes().size());
                }
                g_fg_view = std::make_unique<FactorGraphView>(build_factor_graph_view(tg));
            }

            config.mode = (UncertaintyMode)placer_opts.prob_timing_mode;
            config.alpha = placer_opts.prob_timing_alpha;
            config.beta = placer_opts.prob_timing_beta; 
            config.gamma = crit_params.prob_congestion_gamma; // [PHASE 7]
            config.gamma_b = crit_params.prob_routing_penalty_gamma; // [Option B]
            config.beta2 = placer_opts.prob_timing_beta2; // Fanout-aware variance
            config.beta3 = placer_opts.prob_timing_beta3;

            // [PHASE 7.1] Stability v2
            config.slack_gate = crit_params.prob_slack_gate;
            config.hallucination_dampen = crit_params.prob_hallucination_dampen;
            config.momentum_boost = crit_params.prob_momentum_boost;
            config.min_slack = min_slack; // [PHASE 7.1] Relative Slack

            // [ADAPTIVE] CPD-responsive alpha decay (5% tolerance)
            {
                static double s_best_cpd = 0.0;
                static double s_lambda_scale = 1.0;
                const double TOLERANCE = 0.05;
                const double DECAY = 0.8;
                const double GROWTH = 1.02;
                const double MIN_SCALE = 0.01;
                double current_cpd = (double)det_CPD_s;
                if (s_best_cpd <= 0.0 || g_num_timing_updates_seen <= 1) {
                    s_best_cpd = current_cpd;
                    s_lambda_scale = 1.0;
                } else {
                    if (current_cpd < s_best_cpd) {
                        s_best_cpd = current_cpd;
                        s_lambda_scale = std::min(1.0, s_lambda_scale * GROWTH);
                    } else if (current_cpd > s_best_cpd * (1.0 + TOLERANCE)) {
                        s_lambda_scale *= DECAY;
                        if (s_lambda_scale < MIN_SCALE) s_lambda_scale = MIN_SCALE;
                    }
                }
                config.alpha *= (float)s_lambda_scale;
            }

            // [AUTONOMOUS] apply dynamic scale to lambda noise
            float lambda = placer_opts.prob_inject_lambda * placer_state.runtime().lambda_scale;

            // [PHASE 7] Autonomous Overrides
            if (crit_params.prob_self_calibrate) {
                auto topo = resolve_topological_config(*g_fg_view);
                if (config.alpha <= 0.0f) config.alpha = topo.alpha;
                if (lambda <= 0.0f) lambda = topo.lambda; // Base lambda before ramp
            }

            // [PHASE 7] Dynamic Schedule Ramp — iteration-count based
            // Warmup: first RAMP_WARMUP outer iterations → λ=0, α=0 (pure baseline)
            // Ramp: next RAMP_WARMUP iterations → linearly increase to full values
            // Full: remaining iterations → full λ and α
            // This is benchmark-independent (all benchmarks do 70-300 outer iterations)
            if (crit_params.prob_schedule_ramp) {
                static size_t ramp_call_count = 0;
                ramp_call_count++;

                const size_t RAMP_WARMUP = 30;   // iterations of pure baseline
                const size_t RAMP_TRANSITION = 30; // iterations to ramp from 0 to full

                float ramp = 0.0f;
                if (ramp_call_count <= RAMP_WARMUP) {
                    ramp = 0.0f;
                } else if (ramp_call_count <= RAMP_WARMUP + RAMP_TRANSITION) {
                    ramp = (float)(ramp_call_count - RAMP_WARMUP) / (float)RAMP_TRANSITION;
                } else {
                    ramp = 1.0f;
                }

                lambda *= ramp;
                config.alpha *= ramp;

                if (ramp_call_count % 10 == 0 || ramp_call_count <= 5) {
                    VTR_LOG("SCHEDULE_RAMP: iter=%zu ramp=%.3f λ_eff=%.4f α_eff=%.6f\n",
                            ramp_call_count, ramp, lambda, config.alpha);
                }
            }

            reset_moment_stats();
            // [Option B] Ensure channel utilization is computed when routing penalty is active
            if (config.gamma_b > 1e-9f && crit_params.net_cost_handler) {
                const_cast<NetCostHandler*>(crit_params.net_cost_handler)->estimate_routing_chan_util(false);
            }
            update_physical_state(*g_fg_view, g_phys_state, config, placer_state.block_locs(), crit_params.net_cost_handler);
            summary = run_probabilistic_timing(*g_fg_view, tg, *analyzer, *timing_info->delay_calculator(), g_phys_state, config);

            prob_WNS_s = summary.worst_slack_95;
            prob_worst_node = summary.worst_endpoint_node_id;
            abs_err = std::abs(det_WNS_s - prob_WNS_s);
            same_ep = (det_worst_node == prob_worst_node);

            VTR_LOG("PROB_STEP2_SANITY: update_id=%zu mode=%d alpha=%g beta=%g det_WNS_s=%.15g prob_WorstSlack95_s=%.15g abs_err_WNS_s=%.15g same_worst_ep=%d endpoints=%d ephash=%zu num_eps12=%d num_eps15=%d moments_total=%zu moments_deg=%zu\n",
                    g_num_timing_updates_seen, (int)config.mode, config.alpha, config.beta,
                    (double)det_WNS_s, (double)prob_WNS_s, abs_err, (int)same_ep, (int)summary.num_endpoints, ephash, num_eps12, num_eps15,
                    summary.moments.num_max_calls_total, summary.moments.num_max_calls_sigma_both_zero);

            if (!same_ep) {
                size_t d_node = (det_worst_node != tatum::NodeId::INVALID()) ? (size_t)det_worst_node : 0;
                size_t p_node = (prob_worst_node != tatum::NodeId::INVALID()) ? (size_t)prob_worst_node : 0;
                VTR_LOG("PROB_STEP2_MISMATCH: update_id=%zu det_worst_ep=%zu (slack=%.15g) prob_worst_ep=%zu (slack95=%.15g) num_eps12=%d num_eps15=%d\n",
                        g_num_timing_updates_seen, d_node, (double)min_slack, p_node, (double)prob_WNS_s, num_eps12, num_eps15);
            }

            g_step2_max_abs_err_wns = std::max(g_step2_max_abs_err_wns, abs_err);
            g_step2_sum_abs_err_wns += abs_err;
            if (!same_ep) g_step2_worst_ep_mismatch_count++;

            bool params_are_zero = (config.alpha == 0.0f && config.beta == 0.0f);
            if (params_are_zero && abs_err > 1e-12) {
                VTR_ASSERT_MSG(abs_err <= 1e-12, "PROB_STEP2: Collapse mismatch violation!");
            }
        }

        // 3. Phase 15 Step 3: Replace/Regularize Injection
        if (inject_enabled && (mode == "replace" || mode == "regularize" || mode == "quantile")) {
            if (g_num_injection_updates_seen == 0) {
                VTR_LOG("PROB_STEP3_INJECT_CONFIG: benchmark=%s seed=%d alpha=%g beta=%g inject=on mode=%s lambda=%g clamp=%s\n",
                        g_vpr_ctx.atom().netlist().netlist_name().c_str(), 1, config.alpha, config.beta,
                        mode.c_str(), (double)placer_opts.prob_inject_lambda,
                        placer_opts.prob_inject_clamp ? "on" : "off");
            }
            g_num_injection_updates_seen++;

            double risk_s = (placer_opts.prob_inject_clamp) ? std::max(0.0, -prob_WNS_s) : -prob_WNS_s;
            
            // [NEW] Quantile-Based Pressure (Idea A + PQT)
            if (mode == "quantile" || (mode == "regularize" && placer_opts.prob_inject_quantile > 0.0001f)) {
                float q = (placer_opts.prob_inject_quantile > 0.0001f) ? placer_opts.prob_inject_quantile : 0.05f;
                
                // [PHASE 8] Phase-Aware Quantile Targeting (PQT)
                if (placer_opts.prob_inject_quantile_start > 0.0001f && placer_opts.prob_inject_quantile_end > 0.0001f) {
                    float q_start = placer_opts.prob_inject_quantile_start;
                    float q_end = placer_opts.prob_inject_quantile_end;
                    float t_curr = crit_params.current_temp;
                    
                    if (t_curr > 100.0f) {
                        q = q_start;
                    } else if (t_curr < 1e-6f) {
                        q = q_end;
                    } else {
                        // Logarithmic interpolation for temperature-aware scheduling (more robust)
                        const float log_high = std::log(100.0f);
                        const float log_low = std::log(1e-6f);
                        float progress = (log_high - std::log(t_curr)) / (log_high - log_low);
                        progress = std::max(0.0f, std::min(1.0f, progress));
                        q = q_start + progress * (q_end - q_start);
                    }
                }

                double q_s95 = g_fg_view->get_quantile_slack95(q);
                risk_s = (placer_opts.prob_inject_clamp) ? std::max(0.0, -q_s95) : -q_s95;
                VTR_LOG("PROB_STEP3_QUANTILE: update_id=%zu q=%.4f (T=%.4f) worst_S95=%.6e q_S95=%.6e risk_s=%.6e\n",
                        g_num_timing_updates_seen, (double)q, (double)crit_params.current_temp, prob_WNS_s, q_s95, risk_s);
            }

            double timing_cost_new = timing_cost_before;
            double delta = 0.0;
            // [AUTONOMOUS] apply dynamic scale to lambda noise
            double lambda = (double)(placer_opts.prob_inject_lambda * placer_state.runtime().lambda_scale);

            if (mode == "replace") {
                timing_cost_new = risk_s;
            } else if (mode == "regularize" || mode == "quantile") {
                double ref_s = std::max(1e-15, -(double)det_WNS_s);
                double risk_norm = std::min(5.0, risk_s / ref_s);
                float det_cpd_ns = (float)det_CPD_s * 1e9f;
                
                // [NEW] Populate Adaptive Differential Criticality Matrix BEFORE cost calculation
                if (inject_enabled && g_fg_view) {
                    const auto& cluster_ctx = g_vpr_ctx.clustering();
                    const auto& atom_ctx = g_vpr_ctx.atom();
                    const auto& clb_nlist = cluster_ctx.clb_nlist;
                    
                    if (g_prob_differential_crit.empty()) {
                        g_prob_differential_crit = make_net_pins_matrix<float>(clb_nlist, 0.0f);
                    }

                    double total_diff = 0.0;
                    float max_diff = 0.0f;
                    int diff_p_count = 0;
                    // det_cpd_ns moved to outer scope
                    float max_required = std::max(1e-12f, (float)det_CPD_s);

                    const auto& pin_lookup = criticalities->pin_lookup();
                    for (auto net_id : clb_nlist.nets()) {
                        if (clb_nlist.net_is_ignored(net_id)) continue;
                        
                        // Adaptive Fanout Dampening Factor: 1 / (1 + log(fanout))
                        float fanout = (float)clb_nlist.net_sinks(net_id).size();
                        float fanout_dampener = 1.0f / (1.0f + std::log(std::max(1.0f, fanout)));

                        for (auto pin_id : clb_nlist.net_sinks(net_id)) {
                            int ipin = clb_nlist.pin_net_index(pin_id);
                            
                            float det_crit = 0.0f;
                            float prob_crit = 0.0f;
                            
                            for (auto atom_pin : pin_lookup.connected_atom_pins(pin_id)) {
                                det_crit = std::max(det_crit, timing_info->setup_pin_criticality(atom_pin));

                                tatum::NodeId node_id = atom_ctx.lookup().atom_pin_tnode(atom_pin);
                                if (node_id) {
                                    size_t n_idx = size_t(node_id);
                                    VTR_ASSERT_SAFE_MSG(n_idx < g_fg_view->mu_var_S.size(), "NodeId drift detected in mu_var_S access!");
                                    if (n_idx < g_fg_view->mu_var_S.size()) {
                                        auto moments = g_fg_view->mu_var_S[n_idx];
                                        if (moments.is_set()) {
                                            double p_mu = (double)moments.mu;
                                            double p_std = std::sqrt(std::max(0.0, (double)moments.var));
                                            double p_slack95 = p_mu - 1.64485 * p_std;
                                            float current_prob_crit = 1.0f - (float)(p_slack95 / (double)max_required);
                                            prob_crit = std::max(prob_crit, current_prob_crit);
                                        }
                                    }
                                }
                            }
                            // Differential component scaled by Fanout Dampener
                            float diff = std::max(0.0f, prob_crit - det_crit) * fanout_dampener;

                            // [PHASE 7.1] Census-Gating (Noise Pruning)
                            // Only allow the engine to act as a tie-breaker on paths that are already critical.
                            if (crit_params.prob_census_threshold > 0.0f && det_crit < crit_params.prob_census_threshold) {
                                diff = 0.0f;
                            }

                            g_prob_differential_crit[net_id][ipin] = diff;
                            total_diff += (double)diff;
                            max_diff = std::max(max_diff, diff);
                            if (diff > 0.001) diff_p_count++;
                        }
                    }
                    // [PHASE 7.1] Entropy-Aware Scaling (Confidence Logic)
                    // If the engine is vague (pointing to many pins), dampen its influence.
                    if (crit_params.prob_entropy_sharpening && diff_p_count > 0) {
                        float total_pins = (float)clb_nlist.pins().size();
                        float sparsity = (float)diff_p_count / total_pins;
                        // High sparsity (few pins) = High Confidence (1.0)
                        // Low sparsity (many pins) = Low Confidence (0.05 floor)
                        float confidence = std::max(0.05f, 1.0f - sparsity);
                        lambda *= confidence;
                    }

                    VTR_LOG("!!! ADAPTIVE_P5.1_ACTIVATE !!! total_diff=%.4f max_diff=%.4f pins_with_diff=%d det_cpd=%.4f ns risk_norm=%.4f lambda=%.4f momentum=%.2f\n",
                            total_diff, (double)max_diff, diff_p_count, (double)det_cpd_ns, (double)risk_norm, (double)lambda, g_adaptive_momentum_scaler);
                }

                // [PHASE 7.1] Success-Aware Momentum (Growth Engine)
                // If the engine is helping find real gains, increase its momentum.
                if (g_last_deterministic_cpd > 0.0) {
                    double current_cpd_val = (double)det_cpd_ns;
                    if (current_cpd_val < g_last_deterministic_cpd - 1e-12) {
                        g_adaptive_momentum_scaler *= (double)crit_params.prob_momentum_boost;
                        if (g_adaptive_momentum_scaler > 5.0) g_adaptive_momentum_scaler = 5.0;
                    } else if (current_cpd_val > g_last_deterministic_cpd + 1e-12) {
                        // Decay momentum on regression
                        g_adaptive_momentum_scaler /= (double)crit_params.prob_momentum_boost;
                        if (g_adaptive_momentum_scaler < 1.0) g_adaptive_momentum_scaler = 1.0;
                    }
                }
                g_last_deterministic_cpd = (double)det_cpd_ns;

                // Apply momentum to lambda
                lambda *= (float)g_adaptive_momentum_scaler;

                // [CHANGED] Store normalization factors for connection-level scaling
                g_cached_risk_norm = risk_norm;
                g_cached_prob_lambda = lambda;

                // [OPTION C] Populate per-net congestion cache for uplift scaling
                // Uses separate gamma from Option A (factor graph edge variance)
                g_congestion_uplift_gamma = crit_params.prob_uplift_congestion_gamma;
                if (g_congestion_uplift_gamma > 0.0f && crit_params.net_cost_handler) {
                    // Force channel utilization estimation even if VPR's congestion modeling hasn't started
                    const_cast<NetCostHandler*>(crit_params.net_cost_handler)->estimate_routing_chan_util(false);
                    const auto& clb_nlist = g_vpr_ctx.clustering().clb_nlist;
                    if (g_net_congestion_cache.size() != clb_nlist.nets().size()) {
                        g_net_congestion_cache.resize(clb_nlist.nets().size(), 0.0f);
                    }
                    // Use channel utilization to estimate per-net congestion
                    const auto& chan_util = crit_params.net_cost_handler->get_chan_util();
                    const auto& blk_locs = placer_state.block_locs();
                    for (ClusterNetId nid : clb_nlist.nets()) {
                        // Estimate congestion as average channel util within net's bounding box
                        ClusterBlockId driver = clb_nlist.net_driver_block(nid);
                        auto driver_loc = blk_locs[driver].loc;
                        float max_cong = 0.0f;
                        for (auto pin_id : clb_nlist.net_sinks(nid)) {
                            ClusterBlockId sink = clb_nlist.pin_block(pin_id);
                            auto sink_loc = blk_locs[sink].loc;
                            int x = (driver_loc.x + sink_loc.x) / 2;
                            int y = (driver_loc.y + sink_loc.y) / 2;
                            int layer = driver_loc.layer;
                            if (!chan_util.x.empty() &&
                                layer < (int)chan_util.x.dim_size(0) &&
                                x < (int)chan_util.x.dim_size(1) &&
                                y < (int)chan_util.x.dim_size(2)) {
                                float cx = (float)chan_util.x[layer][x][y];
                                float cy = (float)chan_util.y[layer][x][y];
                                max_cong = std::max(max_cong, (cx + cy) * 0.5f);
                            }
                        }
                        g_net_congestion_cache[nid] = std::min(max_cong, 1.0f);
                    }
                    // [DEBUG] Report congestion cache stats
                    float sum_cong = 0.0f, max_cong_all = 0.0f;
                    int nonzero = 0;
                    for (ClusterNetId nid2 : clb_nlist.nets()) {
                        float c = g_net_congestion_cache[nid2];
                        sum_cong += c;
                        if (c > 0.0f) nonzero++;
                        max_cong_all = std::max(max_cong_all, c);
                    }
                    VTR_LOG("[OPTION_C] gamma=%.2f nets=%zu nonzero_cong=%d max_cong=%.4f avg_cong=%.4f\n",
                            g_congestion_uplift_gamma, clb_nlist.nets().size(), nonzero, max_cong_all,
                            sum_cong / std::max((size_t)1, clb_nlist.nets().size()));
                }
                
                comp_td_costs(delay_model, *criticalities, placer_state, &costs->timing_cost); 
                
                timing_cost_new = costs->timing_cost;
                g_cached_regularization_scaler = 1.0; // Retired
                VTR_LOG("INSTRUMENTATION: Adaptive Differential Criticality Matrix Populated.\n");
            }

            // [NEW] CRITICAL FIX: The connection_timing_cost cache currently holds UN-SCALED costs 
            // (because update_timing_cost ran when scaler was 1.0).
            // We must now scale the cache values so that incremental updates (which read from cache)
            // are consistent with the new global scaler.
            if (mode != "regularize") {
                g_cached_regularization_scaler = 1.0;
                g_cached_risk_norm = 0.0;
                g_cached_prob_lambda = 0.0;
            }

            costs->timing_cost = timing_cost_new;
            timing_cost_after = timing_cost_new;
            delta = timing_cost_new - timing_cost_before;

            if (mode == "replace") {
                VTR_LOG("PROB_STEP3_REPLACE: update_id=%zu prob=%d mode=%d alpha=%g gamma=%g clamp=%d\n",
                        g_num_timing_updates_seen, (int)placer_opts.prob_timing_enable, (int)config.mode, config.alpha, config.gamma, (int)placer_opts.prob_inject_clamp);
                VTR_LOG("PROB_STEP3_REPLACE_STATS: det_WNS=%.6e prob_WNS95=%.6e risk=%.6e cost_before=%.6e cost_new=%.6e delta=%.6e eps=%d\n",
                        (double)det_WNS_s, prob_WNS_s, risk_s, timing_cost_before, timing_cost_new, delta, (int)summary.num_endpoints);
            } else if (mode == "regularize") {
                double ref_s = std::max(1e-15, -(double)det_WNS_s);
                double risk_norm = risk_s / ref_s;
                VTR_LOG("PROB_STEP3_REGULARIZE: update_id=%zu prob=%d mode=%d alpha=%g gamma=%g lambda=%g clamp=%d\n",
                        g_num_timing_updates_seen, (int)placer_opts.prob_timing_enable, (int)config.mode, config.alpha, config.gamma, lambda, (int)placer_opts.prob_inject_clamp);
                VTR_LOG("PROB_STEP3_REGULARIZE_STATS: det_WNS=%.6e prob_WNS95=%.6e risk=%.6e risk_norm=%.6e cost_before=%.6e cost_new=%.6e delta=%.6e eps=%d\n",
                        (double)det_WNS_s, prob_WNS_s, risk_s, risk_norm, timing_cost_before, timing_cost_new, delta, (int)summary.num_endpoints);
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
        // [CHANGED] Cost is now scaled inside comp_td_connection_cost
        *timing_cost = connection_timing_cost.total_cost();
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
            double conn_timing_cost = comp_td_connection_cost(delay_model, place_crit, placer_state, net_id, ipin);

            /* Record new value */
            connection_timing_cost[net_id][ipin] = conn_timing_cost;
        }
        /* Store net timing cost for more efficient incremental updating */
        net_timing_cost[net_id] = sum_td_net_cost(net_id, placer_state);
    }
    /* Make sure timing cost does not go above MIN_TIMING_COST. */
    // [CHANGED] Cost is now scaled inside comp_td_connection_cost
    *timing_cost = sum_td_costs(placer_state);
}

/**
 * @brief Calculates the timing cost of the specified connection.
 *
 * This routine assumes that it is only called either compt_td_cost() or
 * update_td_costs(). Otherwise, various assertions below would fail.
 */
double comp_td_connection_cost(const PlaceDelayModel* delay_model,
                              const PlacerCriticalities& place_crit,
                              const PlacerState& placer_state,
                              ClusterNetId net,
                              int ipin) {
    const auto& block_locs = placer_state.block_locs();

    VTR_ASSERT_SAFE_MSG(ipin > 0, "Shouldn't be calculating connection timing cost for driver pins");

    float delay = comp_td_single_connection_delay(delay_model, block_locs, net, ipin);

    double conn_timing_cost = place_crit.criticality(net, ipin) * delay;


    // [NEW] Apply Slack-Aware Regularization
    // cost = base_cost * (1 + lambda * criticality * risk_norm * geom_scaler)
    if (g_cached_risk_norm > 0.0 && g_cached_prob_lambda > 0.0) {
        // [NEW] Geometric Refinement Logic
        float geom_scaler = 1.0f;
        if (g_cached_prob_dist_func != e_prob_dist_func::LINEAR) {
            auto& cluster_ctx = g_vpr_ctx.clustering();
            const auto& clb_nlist = cluster_ctx.clb_nlist;
            ClusterPinId sink_pin = clb_nlist.net_pin(net, ipin);
            ClusterBlockId sink_block = clb_nlist.pin_block(sink_pin);
            ClusterPinId src_pin = clb_nlist.net_driver(net);
            ClusterBlockId src_block = clb_nlist.pin_block(src_pin);
            t_pl_loc src_loc = block_locs[src_block].loc;
            t_pl_loc sink_loc = block_locs[sink_block].loc;
            int dx = std::abs(src_loc.x - sink_loc.x);
            int dy = std::abs(src_loc.y - sink_loc.y);
            float dist = (float)(dx + dy);

        if (g_cached_prob_dist_func == e_prob_dist_func::STEP) {
                if (dist < g_cached_prob_dist_threshold) {
                    geom_scaler = 0.0f;
                }
            } else if (g_cached_prob_dist_func == e_prob_dist_func::QUADRATIC) {
                geom_scaler = std::max(1.0f, dist); 
            } else if (g_cached_prob_dist_func == e_prob_dist_func::HUBER) {
                float delta = g_cached_prob_huber_delta;
                if (dist <= delta) {
                    geom_scaler = std::max(1.0f, dist); // Quadratic-like (dist scales lambda*crit*dist)
                } else {
                    // Linear extension: cost slope is constant after delta
                    // Scaler formulation: TotalCost ~ (1 + lambda*crit*geom_scaler) * Delay
                    // We want TotalCost to be linear in dist. Delay is ~const.
                    // So geom_scaler should be linear.
                    // Actually, let's look at the math:
                    // Cost = Crit * Delay * (1 + Lambda * GeomScaler)
                    // If GeomScaler ~ Dist, then Cost ~ Dist (Linear).
                    // If GeomScaler ~ Dist^2 (Quadratic implementation elsewhere?), no wait.
                    
                    // Current Implementation (Quadratic Mode):
                    // geom_scaler = dist.
                    // Cost = Crit * Delay * (1 + Lambda * Dist).
                    // This is actually LINEAR in Distance.
                    
                    // Wait. "Quadratic" mode 4 beta=1.0 logic was:
                    // S_e = 1.0 + beta * dist. 
                    // That is Linear scaling of the variance, but since it's applied to the delay prob...
                    // Let's re-read the "Quadratic" implementation in the previous turn.
                    
                    // Re-reading code: 
                    // geom_scaler = dist.
                    // conn_timing_cost *= (1.0 + lambda * crit * geom_scaler)
                    // => Cost ~= C * D * (1 + L * dist)
                    // => Cost ~= C*D + C*D*L*dist.
                    // Since D is roughly proportional to dist (linear delay),
                    // Cost ~= C * (k*dist) + C * (k*dist) * L * dist
                    // Cost ~= k1 * dist + k2 * dist^2.
                    // YES. "Linear" scaler = Quadratic Cost.
                    
                    // So for Huber:
                    // dist <= delta: geom_scaler = dist (Quadratic Cost)
                    // dist > delta:  geom_scaler should ideally make the cost Linear.
                    // To make Cost Linear (Cost ~ k3 * dist), we need (1 + L*geom_scaler) to be Constant?
                    // No. 
                    // Quadratic Cost: k1*d + k2*d^2.
                    // Linear Cost: k1*d.
                    
                    // If we want "Linear Cost" for long nets, we just want standard STA (geom_scaler = 0)?
                    // Or do we want a minimal linear penalty?
                    
                    // Let's stick to the "Gradient" interpretation.
                    // Quadratic Gradient: Force ~ dist.
                    // Linear Gradient: Force ~ constant.
                    
                    // If geom_scaler = constant (e.g. delta), then Cost ~= k1*d + k2*d*delta.
                    // This is Linear in d.
                    
                    geom_scaler = delta;
                }
            }
        }

        // [TUNED] Logic-Depth Confidence Scaling Removed for Synchronization Audit
        float lambda_eff = (float)g_cached_prob_lambda;
        // [TUNED] Capping removed for Synchronization Audit

        // [NEW] Topology Dampening & Linearized Criticality
        float crit_diff = 0.0f;
        if (!g_prob_differential_crit.empty()) {
            if (size_t(net) < g_prob_differential_crit.size() && size_t(ipin) < g_prob_differential_crit[net].size()) {
                crit_diff = g_prob_differential_crit[net][ipin];
            }
        }

        // [TUNED] Simplified Linear Scaler for Synchronization with "Original Inference Method"
        // Only apply if PQT is explicitly enabled to ensure 100% clean baseline.
        if (g_cached_prob_lambda > 0.0) {
            // [OPTION C] Scale down uplift in congested regions to prevent routing reversals
            if (g_congestion_uplift_gamma > 0.0f && !g_net_congestion_cache.empty() &&
                size_t(net) < g_net_congestion_cache.size()) {
                float cong = g_net_congestion_cache[net];
                // Reduce uplift: high congestion → less timing pressure
                // crit_diff *= (1 - gamma * congestion)
                crit_diff *= std::max(0.0f, 1.0f - g_congestion_uplift_gamma * cong);
            }
            double scaler = 1.0 + (double)lambda_eff * (double)crit_diff;
            conn_timing_cost *= scaler;
        }
    }

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

// =========================================================================
// Factor-Graph Proxy Checkpoint Instrumentation
// =========================================================================

// Cache for previous-iteration uplifts (for drift computation)
static ClbNetPinsMatrix<float> g_proxy_prev_uplift;
static bool g_proxy_prev_valid = false;

void emit_factor_graph_proxy_checkpoint(
    int iteration,
    SetupTimingInfo* timing_info,
    const PlacerCriticalities* criticalities,
    const PlacerState& placer_state,
    const t_placer_opts& placer_opts,
    const std::string& output_path,
    double bb_cost,
    double timing_cost,
    double total_cost,
    double bb_cost_norm,
    double timing_cost_norm) {

    if (!g_fg_view || output_path.empty()) return;

    auto t_checkpoint_start = std::chrono::steady_clock::now();

    const auto& cluster_ctx = g_vpr_ctx.clustering();
    const auto& atom_ctx    = g_vpr_ctx.atom();
    const auto& clb_nlist   = cluster_ctx.clb_nlist;
    const auto& tg          = *timing_info->timing_graph();
    const auto& pin_lookup  = criticalities->pin_lookup();

    // max_required is computed after Pass 1 where det_CPD_s is available

    // --- Pass 0: Compute per-net HPWL from current placement ---
    const auto& block_locs = placer_state.block_locs();
    std::unordered_map<size_t, double> net_hpwl_map;
    std::vector<double> all_net_hpwls;
    all_net_hpwls.reserve(clb_nlist.nets().size());

    for (auto net_id : clb_nlist.nets()) {
        if (clb_nlist.net_is_ignored(net_id)) continue;
        const auto& net_pins = clb_nlist.net_pins(net_id);
        if (net_pins.size() < 2) continue;

        auto src_blk = clb_nlist.net_driver_block(net_id);
        auto src_loc = block_locs[src_blk].loc;
        int min_x = src_loc.x, max_x = src_loc.x;
        int min_y = src_loc.y, max_y = src_loc.y;

        for (size_t ipin = 1; ipin < net_pins.size(); ipin++) {
            auto pin_blk = clb_nlist.net_pin_block(net_id, ipin);
            auto pin_loc = block_locs[pin_blk].loc;
            if (pin_loc.x < min_x) min_x = pin_loc.x;
            if (pin_loc.x > max_x) max_x = pin_loc.x;
            if (pin_loc.y < min_y) min_y = pin_loc.y;
            if (pin_loc.y > max_y) max_y = pin_loc.y;
        }
        double hpwl = (double)(max_x - min_x) + (double)(max_y - min_y);
        net_hpwl_map[(size_t)net_id] = hpwl;
        all_net_hpwls.push_back(hpwl);
    }

    // Net BB distribution stats
    std::sort(all_net_hpwls.begin(), all_net_hpwls.end());
    size_t nn = all_net_hpwls.size();
    double net_bb_mean = 0.0, net_bb_std = 0.0;
    double net_bb_p50 = 0.0, net_bb_p90 = 0.0, net_bb_p99 = 0.0, net_bb_max = 0.0;
    if (nn > 0) {
        double s = 0.0;
        for (double v : all_net_hpwls) s += v;
        net_bb_mean = s / nn;
        double sq = 0.0;
        for (double v : all_net_hpwls) sq += (v - net_bb_mean) * (v - net_bb_mean);
        net_bb_std = std::sqrt(sq / nn);
        net_bb_p50 = all_net_hpwls[nn / 2];
        net_bb_p90 = all_net_hpwls[std::min(nn - 1, (size_t)(nn * 0.90))];
        net_bb_p99 = all_net_hpwls[std::min(nn - 1, (size_t)(nn * 0.99))];
        net_bb_max = all_net_hpwls.back();
    }

    // --- Pass 1: Compute per-connection uplift + hybrid metrics ---
    std::vector<float> all_uplifts;
    all_uplifts.reserve(clb_nlist.nets().size() * 4);

    // Existing accumulators
    double sum_uplift = 0.0;
    double sum_positive_uplift = 0.0;
    double drift_sum_abs = 0.0;
    size_t num_edges_scored = 0;
    bool drift_valid = g_proxy_prev_valid && (iteration > 0);

    // Hybrid accumulators: factor-graph × placement
    double sum_uplift_times_hpwl = 0.0;  // for uplift-weighted BB
    double sum_pos_uplift_for_wbb = 0.0;
    double sum_crit_times_hpwl = 0.0;    // for crit-weighted BB
    double sum_crit_for_wbb = 0.0;
    size_t num_crit_conns = 0;           // connections with det_crit > 0.9

    // For correlation: store per-connection (uplift, hpwl) pairs
    std::vector<float> conn_uplifts_vec;
    std::vector<double> conn_hpwls_vec;
    conn_uplifts_vec.reserve(clb_nlist.nets().size() * 4);
    conn_hpwls_vec.reserve(clb_nlist.nets().size() * 4);

    // For high-uplift BB analysis
    struct UpliftBB { float uplift; double hpwl; };
    std::vector<UpliftBB> uplift_bb_pairs;
    uplift_bb_pairs.reserve(clb_nlist.nets().size() * 4);

    // Number of critical nets (max pin crit > 0.9)
    size_t num_crit_nets = 0;

    // Initialize prev cache if needed
    if (g_proxy_prev_uplift.empty()) {
        g_proxy_prev_uplift = make_net_pins_matrix<float>(clb_nlist, 0.0f);
    }

    for (auto net_id : clb_nlist.nets()) {
        if (clb_nlist.net_is_ignored(net_id)) continue;

        double net_hpwl = 0.0;
        auto it = net_hpwl_map.find((size_t)net_id);
        if (it != net_hpwl_map.end()) net_hpwl = it->second;

        float net_max_det_crit = 0.0f;

        for (auto pin_id : clb_nlist.net_sinks(net_id)) {
            int ipin = clb_nlist.pin_net_index(pin_id);
            float uplift = g_prob_differential_crit[net_id][ipin];
            all_uplifts.push_back(uplift);
            sum_uplift += (double)uplift;
            if (uplift > 0.0f) sum_positive_uplift += (double)uplift;
            num_edges_scored++;

            if (drift_valid) {
                drift_sum_abs += std::abs((double)uplift - (double)g_proxy_prev_uplift[net_id][ipin]);
            }
            g_proxy_prev_uplift[net_id][ipin] = uplift;

            // Get deterministic criticality for this connection
            float det_crit = criticalities->criticality(net_id, ipin);
            net_max_det_crit = std::max(net_max_det_crit, det_crit);
            if (det_crit > 0.9f) num_crit_conns++;

            // Hybrid: accumulate uplift×hpwl and crit×hpwl
            if (uplift > 0.0f) {
                sum_uplift_times_hpwl += (double)uplift * net_hpwl;
                sum_pos_uplift_for_wbb += (double)uplift;
            }
            sum_crit_times_hpwl += (double)det_crit * net_hpwl;
            sum_crit_for_wbb += (double)det_crit;

            // Store for correlation and top-k analysis
            conn_uplifts_vec.push_back(uplift);
            conn_hpwls_vec.push_back(net_hpwl);
            uplift_bb_pairs.push_back({uplift, net_hpwl});
        }

        if (net_max_det_crit > 0.9f) num_crit_nets++;
    }

    // Compute hybrid features
    double uplift_weighted_bb = (sum_pos_uplift_for_wbb > 0.0)
        ? sum_uplift_times_hpwl / sum_pos_uplift_for_wbb : 0.0;
    double crit_weighted_bb = (sum_crit_for_wbb > 0.0)
        ? sum_crit_times_hpwl / sum_crit_for_wbb : 0.0;

    // High-uplift mean BB: sort by uplift desc, take top 10% mean BB
    std::sort(uplift_bb_pairs.begin(), uplift_bb_pairs.end(),
              [](const UpliftBB& a, const UpliftBB& b) { return a.uplift > b.uplift; });
    double high_uplift_mean_bb = 0.0;
    double low_uplift_mean_bb = 0.0;
    if (!uplift_bb_pairs.empty()) {
        size_t top10pct = std::max((size_t)1, uplift_bb_pairs.size() / 10);
        double s = 0.0;
        for (size_t i = 0; i < top10pct; i++) s += uplift_bb_pairs[i].hpwl;
        high_uplift_mean_bb = s / top10pct;
        double s2 = 0.0;
        size_t cnt = 0;
        for (size_t i = top10pct; i < uplift_bb_pairs.size(); i++) {
            s2 += uplift_bb_pairs[i].hpwl;
            cnt++;
        }
        low_uplift_mean_bb = (cnt > 0) ? s2 / cnt : 0.0;
    }

    // Pearson correlation between uplift and HPWL
    double uplift_bb_pearson = 0.0;
    {
        size_t nc = conn_uplifts_vec.size();
        if (nc > 2) {
            double su = 0, sh = 0;
            for (size_t i = 0; i < nc; i++) { su += conn_uplifts_vec[i]; sh += conn_hpwls_vec[i]; }
            double mu = su / nc, mh = sh / nc;
            double cov = 0, vu = 0, vh = 0;
            for (size_t i = 0; i < nc; i++) {
                double du = conn_uplifts_vec[i] - mu;
                double dh = conn_hpwls_vec[i] - mh;
                cov += du * dh;
                vu += du * du;
                vh += dh * dh;
            }
            double denom = std::sqrt(vu * vh);
            uplift_bb_pearson = (denom > 0.0) ? cov / denom : 0.0;
        }
    }

    // Cost deltas from iteration 0 (static cache)
    static double cached_bb_cost_iter0 = 0.0;
    static double cached_timing_cost_iter0 = 0.0;
    static double cached_pre_route_cpd_iter0 = 0.0;
    float det_CPD_s = timing_info->least_slack_critical_path().delay().value();
    if (iteration == 0) {
        cached_bb_cost_iter0 = bb_cost;
        cached_timing_cost_iter0 = timing_cost;
        cached_pre_route_cpd_iter0 = (double)det_CPD_s;
    }
    double bb_cost_delta = bb_cost - cached_bb_cost_iter0;
    double timing_cost_delta = timing_cost - cached_timing_cost_iter0;
    double pre_route_cpd_delta = (double)det_CPD_s - cached_pre_route_cpd_iter0;

    double mean_crit_uplift = (num_edges_scored > 0) ? sum_uplift / num_edges_scored : 0.0;

    // --- Feature 2: Activated Mass ---
    // Sort uplifts descending to find percentile thresholds
    std::vector<float> sorted_uplifts = all_uplifts;
    std::sort(sorted_uplifts.begin(), sorted_uplifts.end(), std::greater<float>());

    auto compute_activated_mass = [&](float pct) -> double {
        if (sum_positive_uplift <= 0.0 || sorted_uplifts.empty()) return -1.0; // NULL sentinel
        size_t k = std::max((size_t)1, (size_t)(sorted_uplifts.size() * pct));
        double top_sum = 0.0;
        for (size_t i = 0; i < k && i < sorted_uplifts.size(); i++) {
            top_sum += std::max(0.0f, sorted_uplifts[i]);
        }
        return top_sum / sum_positive_uplift;
    };

    double am_1  = compute_activated_mass(0.01f);
    double am_2  = compute_activated_mass(0.02f);
    double am_5  = compute_activated_mass(0.05f);
    double am_10 = compute_activated_mass(0.10f);

    // --- Pass 2: Timing-graph node metrics ---
    // Reconvergence pressure + tight competition fraction
    // Using g_fg_view->mu_var_B[edge] for candidate arrivals

    size_t num_timing_nodes = g_fg_view->num_nodes;
    size_t num_multiinput_nodes = 0;
    double max_arrival_global = 0.0;
    double sum_arrival = 0.0;
    double sum_max_fanin_minus1 = 0.0; // upper bound for reconvergence pressure

    int rp_1 = 0, rp_2 = 0, rp_5 = 0, rp_10 = 0;
    int tcf_1_num = 0, tcf_2_num = 0, tcf_5_num = 0, tcf_10_num = 0;

    size_t nodes_with_arrivals = 0;
    for (size_t n = 0; n < num_timing_nodes; n++) {
        const auto& in_e = g_fg_view->in_edges[n];
        if (in_e.empty()) continue;

        // Compute max arrival among incoming edges
        double m_max = -std::numeric_limits<double>::infinity();
        std::vector<double> arrivals;
        arrivals.reserve(in_e.size());

        for (auto eid : in_e) {
            size_t ei = (size_t)eid;
            if (ei < g_fg_view->mu_var_B.size() && g_fg_view->mu_var_B[ei].is_set()) {
                double a = g_fg_view->mu_var_B[ei].mu;
                arrivals.push_back(a);
                m_max = std::max(m_max, a);
            }
        }

        if (arrivals.empty() || !std::isfinite(m_max)) continue;

        nodes_with_arrivals++;
        sum_arrival += m_max;
        max_arrival_global = std::max(max_arrival_global, m_max);

        if (arrivals.size() < 2) continue;

        num_multiinput_nodes++;
        sum_max_fanin_minus1 += (double)(arrivals.size() - 1);

        // Sort descending for top-2 and reconvergence
        std::sort(arrivals.begin(), arrivals.end(), std::greater<double>());
        double m1 = arrivals[0];
        double m2 = arrivals[1];
        double margin = m1 - m2;

        // Reconvergence pressure: count near-critical incoming edges
        double thresholds[] = {0.01, 0.02, 0.05, 0.10};
        int rp_counts[4] = {0, 0, 0, 0};
        for (double a : arrivals) {
            for (int t = 0; t < 4; t++) {
                if (m_max - a <= thresholds[t] * std::abs(m_max)) {
                    rp_counts[t]++;
                }
            }
        }
        rp_1  += std::max(0, rp_counts[0] - 1);
        rp_2  += std::max(0, rp_counts[1] - 1);
        rp_5  += std::max(0, rp_counts[2] - 1);
        rp_10 += std::max(0, rp_counts[3] - 1);

        // Tight competition fraction
        if (m1 > 0.0) {
            if (margin <= 0.01 * m1) tcf_1_num++;
            if (margin <= 0.02 * m1) tcf_2_num++;
            if (margin <= 0.05 * m1) tcf_5_num++;
            if (margin <= 0.10 * m1) tcf_10_num++;
        }
    }

    double mean_arrival_global = (nodes_with_arrivals > 0) ? sum_arrival / nodes_with_arrivals : 0.0;
    double tcf_1  = (num_multiinput_nodes > 0) ? (double)tcf_1_num  / num_multiinput_nodes : 0.0;
    double tcf_2  = (num_multiinput_nodes > 0) ? (double)tcf_2_num  / num_multiinput_nodes : 0.0;
    double tcf_5  = (num_multiinput_nodes > 0) ? (double)tcf_5_num  / num_multiinput_nodes : 0.0;
    double tcf_10 = (num_multiinput_nodes > 0) ? (double)tcf_10_num / num_multiinput_nodes : 0.0;

    // --- Feature 5: Uplift Drift ---
    static double g_proxy_prev_mean_uplift = 0.0;
    double uplift_drift_mean = -1.0;  // NULL sentinel
    double uplift_drift_field = -1.0; // NULL sentinel
    if (iteration > 0 && drift_valid) {
        uplift_drift_mean = std::abs(mean_crit_uplift - g_proxy_prev_mean_uplift);
        uplift_drift_field = (num_edges_scored > 0) ? drift_sum_abs / num_edges_scored : 0.0;
    }
    g_proxy_prev_mean_uplift = mean_crit_uplift;
    g_proxy_prev_valid = true;

    // --- Sanity Checks ---
    bool sum_positive_uplift_zero = (sum_positive_uplift <= 0.0);
    if (sum_positive_uplift_zero) {
        am_1 = am_2 = am_5 = am_10 = -1.0; // NULL
    }

    // Activated mass bounds
    bool am_1_valid = (am_1 < 0) || (am_1 >= 0.0 && am_1 <= 1.0 + 1e-9);
    bool am_2_valid = (am_2 < 0) || (am_2 >= 0.0 && am_2 <= 1.0 + 1e-9);
    bool am_5_valid = (am_5 < 0) || (am_5 >= 0.0 && am_5 <= 1.0 + 1e-9);
    bool am_10_valid = (am_10 < 0) || (am_10 >= 0.0 && am_10 <= 1.0 + 1e-9);

    // TCF bounds
    bool tcf_1_valid = (tcf_1 >= 0.0 && tcf_1 <= 1.0 + 1e-9);
    bool tcf_2_valid = (tcf_2 >= 0.0 && tcf_2 <= 1.0 + 1e-9);
    bool tcf_5_valid = (tcf_5 >= 0.0 && tcf_5 <= 1.0 + 1e-9);
    bool tcf_10_valid = (tcf_10 >= 0.0 && tcf_10 <= 1.0 + 1e-9);

    // RP bounds
    bool rp_1_valid = (rp_1 >= 0 && (double)rp_1 <= sum_max_fanin_minus1 + 1);
    bool rp_2_valid = (rp_2 >= 0 && (double)rp_2 <= sum_max_fanin_minus1 + 1);
    bool rp_5_valid = (rp_5 >= 0 && (double)rp_5 <= sum_max_fanin_minus1 + 1);
    bool rp_10_valid = (rp_10 >= 0 && (double)rp_10 <= sum_max_fanin_minus1 + 1);

    // Monotonicity
    bool am_mono = (am_1 < 0) || (am_1 <= am_2 + 1e-9 && am_2 <= am_5 + 1e-9 && am_5 <= am_10 + 1e-9);
    bool rp_mono = (rp_1 <= rp_2 && rp_2 <= rp_5 && rp_5 <= rp_10);
    bool tcf_mono = (tcf_1 <= tcf_2 + 1e-9 && tcf_2 <= tcf_5 + 1e-9 && tcf_5 <= tcf_10 + 1e-9);

    // Drift checks
    bool drift_null_ok = (iteration == 0) ? (uplift_drift_mean < 0 && uplift_drift_field < 0)
                                          : (uplift_drift_mean >= 0 && uplift_drift_field >= 0);
    bool drift_nonneg = (iteration == 0) || (uplift_drift_mean >= 0 && uplift_drift_field >= 0);

    // Row validity
    bool row_valid = am_1_valid && am_2_valid && am_5_valid && am_10_valid
                  && tcf_1_valid && tcf_2_valid && tcf_5_valid && tcf_10_valid
                  && rp_1_valid && rp_2_valid && rp_5_valid && rp_10_valid
                  && am_mono && rp_mono && tcf_mono && drift_null_ok && drift_nonneg;

    // --- Write CSV ---
    bool write_header = false;
    {
        FILE* test = fopen(output_path.c_str(), "r");
        if (!test) {
            write_header = true;
        } else {
            fclose(test);
        }
    }

    FILE* fp = fopen(output_path.c_str(), "a");
    if (!fp) {
        VTR_LOG_ERROR("PROXY_CHECKPOINT: Cannot open %s for writing\n", output_path.c_str());
        return;
    }

    if (write_header) {
        fprintf(fp, "iteration,"
            "pre_route_cpd,"
            "bb_cost,timing_cost,total_cost,bb_cost_norm,timing_cost_norm,"
            "bb_cost_delta,timing_cost_delta,pre_route_cpd_delta,"
            "net_bb_mean,net_bb_p50,net_bb_p90,net_bb_p99,net_bb_max,net_bb_std,"
            "num_crit_nets,num_crit_conns,"
            "uplift_weighted_bb,crit_weighted_bb,"
            "high_uplift_mean_bb,low_uplift_mean_bb,"
            "uplift_bb_pearson,"
            "mean_crit_uplift,"
            "activated_mass_1pct,activated_mass_2pct,activated_mass_5pct,activated_mass_10pct,"
            "reconvergence_pressure_1,reconvergence_pressure_2,reconvergence_pressure_5,reconvergence_pressure_10,"
            "tight_competition_fraction_1,tight_competition_fraction_2,tight_competition_fraction_5,tight_competition_fraction_10,"
            "uplift_drift_mean,uplift_drift_field,"
            "num_edges_scored,num_timing_nodes,num_multiinput_nodes,"
            "sum_positive_uplift,max_arrival_global,mean_arrival_global,"
            "activated_mass_1_valid,activated_mass_2_valid,activated_mass_5_valid,activated_mass_10_valid,"
            "tight_competition_fraction_1_valid,tight_competition_fraction_2_valid,tight_competition_fraction_5_valid,tight_competition_fraction_10_valid,"
            "reconvergence_pressure_1_valid,reconvergence_pressure_2_valid,reconvergence_pressure_5_valid,reconvergence_pressure_10_valid,"
            "activated_mass_monotonic_flag,reconvergence_pressure_monotonic_flag,tight_competition_monotonic_flag,"
            "uplift_drift_nullability_flag,uplift_drift_nonnegative_flag,"
            "sum_positive_uplift_zero_flag,"
            "row_valid_flag,"
            "checkpoint_compute_ms\n");
    }

    auto fmtf = [](double v) -> std::string {
        if (v < 0) return ""; // NULL
        char buf[64];
        snprintf(buf, sizeof(buf), "%.9g", v);
        return buf;
    };

    auto t_checkpoint_end = std::chrono::steady_clock::now();
    double checkpoint_ms = std::chrono::duration<double, std::milli>(t_checkpoint_end - t_checkpoint_start).count();

    // pre_route_cpd from det_CPD_s computed in Pass 1
    double pre_route_cpd = (double)det_CPD_s;

    fprintf(fp, "%d,"
        "%.9g,"
        "%.9g,%.9g,%.9g,%.9g,%.9g,"
        "%.9g,%.9g,%.9g,"
        "%.9g,%.9g,%.9g,%.9g,%.9g,%.9g,"
        "%zu,%zu,"
        "%.9g,%.9g,"
        "%.9g,%.9g,"
        "%.9g,"
        "%.9g,"
        "%s,%s,%s,%s,"
        "%d,%d,%d,%d,"
        "%.9g,%.9g,%.9g,%.9g,"
        "%s,%s,"
        "%zu,%zu,%zu,"
        "%.9g,%.9g,%.9g,"
        "%d,%d,%d,%d,"
        "%d,%d,%d,%d,"
        "%d,%d,%d,%d,"
        "%d,%d,%d,"
        "%d,%d,"
        "%d,"
        "%d,"
        "%.3f\n",
        iteration,
        pre_route_cpd,
        bb_cost, timing_cost, total_cost, bb_cost_norm, timing_cost_norm,
        bb_cost_delta, timing_cost_delta, pre_route_cpd_delta,
        net_bb_mean, net_bb_p50, net_bb_p90, net_bb_p99, net_bb_max, net_bb_std,
        num_crit_nets, num_crit_conns,
        uplift_weighted_bb, crit_weighted_bb,
        high_uplift_mean_bb, low_uplift_mean_bb,
        uplift_bb_pearson,
        mean_crit_uplift,
        fmtf(am_1).c_str(), fmtf(am_2).c_str(), fmtf(am_5).c_str(), fmtf(am_10).c_str(),
        rp_1, rp_2, rp_5, rp_10,
        tcf_1, tcf_2, tcf_5, tcf_10,
        fmtf(uplift_drift_mean).c_str(), fmtf(uplift_drift_field).c_str(),
        num_edges_scored, num_timing_nodes, num_multiinput_nodes,
        sum_positive_uplift, max_arrival_global, mean_arrival_global,
        (int)am_1_valid, (int)am_2_valid, (int)am_5_valid, (int)am_10_valid,
        (int)tcf_1_valid, (int)tcf_2_valid, (int)tcf_5_valid, (int)tcf_10_valid,
        (int)rp_1_valid, (int)rp_2_valid, (int)rp_5_valid, (int)rp_10_valid,
        (int)am_mono, (int)rp_mono, (int)tcf_mono,
        (int)drift_null_ok, (int)drift_nonneg,
        (int)sum_positive_uplift_zero,
        (int)row_valid,
        checkpoint_ms);

    fclose(fp);

    VTR_LOG("PROXY_CHECKPOINT: iter=%d edges=%zu nodes=%zu multi=%zu mean_uplift=%.6g am1=%.4f rp1=%d tcf1=%.4f valid=%d compute_ms=%.3f\n",
            iteration, num_edges_scored, num_timing_nodes, num_multiinput_nodes,
            mean_crit_uplift, (am_1 >= 0 ? am_1 : 0.0), rp_1, tcf_1, (int)row_valid, checkpoint_ms);
}

void emit_geometry_only_proxy_checkpoint(
    int iteration,
    const PlacerState& placer_state,
    const std::string& output_path,
    double bb_cost) {

    if (output_path.empty()) return;

    auto t_checkpoint_start = std::chrono::steady_clock::now();

    const auto& cluster_ctx = g_vpr_ctx.clustering();
    const auto& clb_nlist   = cluster_ctx.clb_nlist;
    const auto& block_locs  = placer_state.block_locs();

    // Compute per-net HPWL from current placement
    std::vector<double> all_net_hpwls;
    all_net_hpwls.reserve(clb_nlist.nets().size());

    for (auto net_id : clb_nlist.nets()) {
        if (clb_nlist.net_is_ignored(net_id)) continue;
        const auto& net_pins = clb_nlist.net_pins(net_id);
        if (net_pins.size() < 2) continue;

        auto src_blk = clb_nlist.net_driver_block(net_id);
        auto src_loc = block_locs[src_blk].loc;
        int min_x = src_loc.x, max_x = src_loc.x;
        int min_y = src_loc.y, max_y = src_loc.y;

        for (size_t ipin = 1; ipin < net_pins.size(); ipin++) {
            auto pin_blk = clb_nlist.net_pin_block(net_id, ipin);
            auto pin_loc = block_locs[pin_blk].loc;
            if (pin_loc.x < min_x) min_x = pin_loc.x;
            if (pin_loc.x > max_x) max_x = pin_loc.x;
            if (pin_loc.y < min_y) min_y = pin_loc.y;
            if (pin_loc.y > max_y) max_y = pin_loc.y;
        }
        double hpwl = (double)(max_x - min_x) + (double)(max_y - min_y);
        all_net_hpwls.push_back(hpwl);
    }

    // Net BB distribution stats
    std::sort(all_net_hpwls.begin(), all_net_hpwls.end());
    size_t nn = all_net_hpwls.size();
    double net_bb_mean = 0.0, net_bb_std = 0.0;
    double net_bb_p50 = 0.0, net_bb_p90 = 0.0, net_bb_p99 = 0.0, net_bb_max = 0.0;
    if (nn > 0) {
        double s = 0.0;
        for (double v : all_net_hpwls) s += v;
        net_bb_mean = s / nn;
        double sq = 0.0;
        for (double v : all_net_hpwls) sq += (v - net_bb_mean) * (v - net_bb_mean);
        net_bb_std = std::sqrt(sq / nn);
        net_bb_p50 = all_net_hpwls[nn / 2];
        net_bb_p90 = all_net_hpwls[std::min(nn - 1, (size_t)(nn * 0.90))];
        net_bb_p99 = all_net_hpwls[std::min(nn - 1, (size_t)(nn * 0.99))];
        net_bb_max = all_net_hpwls.back();
    }

    auto t_checkpoint_end = std::chrono::steady_clock::now();
    double checkpoint_ms = std::chrono::duration<double, std::milli>(t_checkpoint_end - t_checkpoint_start).count();

    // Write CSV with same header as full checkpoint, zeros for timing/FG columns
    bool write_header = false;
    {
        FILE* test = fopen(output_path.c_str(), "r");
        if (!test) {
            write_header = true;
        } else {
            fclose(test);
        }
    }

    FILE* fp = fopen(output_path.c_str(), "a");
    if (!fp) {
        VTR_LOG_ERROR("PROXY_CHECKPOINT_GEO: Cannot open %s for writing\n", output_path.c_str());
        return;
    }

    if (write_header) {
        fprintf(fp, "iteration,"
            "pre_route_cpd,"
            "bb_cost,timing_cost,total_cost,bb_cost_norm,timing_cost_norm,"
            "bb_cost_delta,timing_cost_delta,pre_route_cpd_delta,"
            "net_bb_mean,net_bb_p50,net_bb_p90,net_bb_p99,net_bb_max,net_bb_std,"
            "num_crit_nets,num_crit_conns,"
            "uplift_weighted_bb,crit_weighted_bb,"
            "high_uplift_mean_bb,low_uplift_mean_bb,"
            "uplift_bb_pearson,"
            "mean_crit_uplift,"
            "activated_mass_1pct,activated_mass_2pct,activated_mass_5pct,activated_mass_10pct,"
            "reconvergence_pressure_1,reconvergence_pressure_2,reconvergence_pressure_5,reconvergence_pressure_10,"
            "tight_competition_fraction_1,tight_competition_fraction_2,tight_competition_fraction_5,tight_competition_fraction_10,"
            "uplift_drift_mean,uplift_drift_field,"
            "num_edges_scored,num_timing_nodes,num_multiinput_nodes,"
            "sum_positive_uplift,max_arrival_global,mean_arrival_global,"
            "activated_mass_1_valid,activated_mass_2_valid,activated_mass_5_valid,activated_mass_10_valid,"
            "tight_competition_fraction_1_valid,tight_competition_fraction_2_valid,tight_competition_fraction_5_valid,tight_competition_fraction_10_valid,"
            "reconvergence_pressure_1_valid,reconvergence_pressure_2_valid,reconvergence_pressure_5_valid,reconvergence_pressure_10_valid,"
            "activated_mass_monotonic_flag,reconvergence_pressure_monotonic_flag,tight_competition_monotonic_flag,"
            "uplift_drift_nullability_flag,uplift_drift_nonnegative_flag,"
            "sum_positive_uplift_zero_flag,"
            "row_valid_flag,"
            "checkpoint_compute_ms\n");
    }

    // Write row: geometry columns have real values, all timing/FG columns are 0
    fprintf(fp, "%d,"
        "0,"                                             // pre_route_cpd
        "%.9g,0,0,0,0,"                                 // bb_cost, timing_cost, total_cost, bb_cost_norm, timing_cost_norm
        "0,0,0,"                                         // deltas
        "%.9g,%.9g,%.9g,%.9g,%.9g,%.9g,"                // net_bb stats
        "0,0,"                                           // num_crit_nets, num_crit_conns
        "0,0,"                                           // uplift_weighted_bb, crit_weighted_bb
        "0,0,"                                           // high_uplift_mean_bb, low_uplift_mean_bb
        "0,"                                             // uplift_bb_pearson
        "0,"                                             // mean_crit_uplift
        ",,,,"                                           // activated_mass 1,2,5,10 (NULL)
        "0,0,0,0,"                                       // reconvergence_pressure
        "0,0,0,0,"                                       // tight_competition_fraction
        ",,"                                             // uplift_drift (NULL)
        "0,0,0,"                                         // num_edges_scored, num_timing_nodes, num_multiinput_nodes
        "0,0,0,"                                         // sum_positive_uplift, max_arrival_global, mean_arrival_global
        "1,1,1,1,"                                       // am valid flags
        "1,1,1,1,"                                       // tcf valid flags
        "1,1,1,1,"                                       // rp valid flags
        "1,1,1,"                                         // monotonic flags
        "1,1,"                                           // drift flags
        "1,"                                             // sum_positive_uplift_zero_flag
        "1,"                                             // row_valid_flag
        "%.3f\n",
        iteration,
        bb_cost,
        net_bb_mean, net_bb_p50, net_bb_p90, net_bb_p99, net_bb_max, net_bb_std,
        checkpoint_ms);

    fclose(fp);

    VTR_LOG("PROXY_CHECKPOINT_GEO: iter=%d nets=%zu net_bb_std=%.4f net_bb_p90=%.4f bb_cost=%.4f compute_ms=%.3f\n",
            iteration, nn, net_bb_std, net_bb_p90, bb_cost, checkpoint_ms);
}
