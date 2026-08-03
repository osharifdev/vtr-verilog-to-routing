#include "FactorGraphView.h"
#include "tatum/TimingGraph.hpp"
#include "tatum/analyzers/SetupTimingAnalyzer.hpp"
#include "tatum/delay_calc/DelayCalculator.hpp"
#include "vtr_log.h"
#include "globals.h"
#include "atom_netlist.h"
#include "net_cost_handler.h"
#include "vpr_utils.h"
#include <algorithm>
#include <iostream>
#include <iomanip>
#include <cmath>
#include <chrono>

/*
 * Math Helpers
 */
static double normal_pdf(double x) {
    constexpr double inv_sqrt_2pi = 0.3989422804014327; // 1/sqrt(2*pi)
    return inv_sqrt_2pi * std::exp(-0.5 * x * x);
}

static double normal_cdf(double x) {
    return 0.5 * std::erfc(-x * 0.7071067811865476); // 1/sqrt(2)
}

static MomentStats g_moment_stats;

void reset_moment_stats() {
    g_moment_stats = MomentStats();
}

MomentStats get_moment_stats() {
    return g_moment_stats;
}

static GaussianMoments max_gaussian_moments(GaussianMoments a, GaussianMoments b) {
    g_moment_stats.num_max_calls_total++;
    if (!a.is_set()) return b;
    if (!b.is_set()) return a;

    if (a.var <= 0.0 && b.var <= 0.0) {
        g_moment_stats.num_max_calls_sigma_both_zero++;
        return (a.mu > b.mu) ? a : b;
    }

    if (a.var <= 0.0 || b.var <= 0.0) {
        g_moment_stats.num_max_calls_sigma_one_zero++;
    } else {
        g_moment_stats.num_max_calls_general++;
    }

    double var_diff = a.var + b.var;
    double delta = std::sqrt(std::max(1e-24, var_diff));

    // Numerical stability
    if (delta < 1e-12) {
        return (a.mu > b.mu) ? a : b;
    }

    double alpha = (a.mu - b.mu) / delta;

    // Numerical stability for large alpha
    if (alpha > 8.0) return a;
    if (alpha < -8.0) return b;

    double Phi = normal_cdf(alpha);
    double phi = normal_pdf(alpha);

    double mu_Z = a.mu * Phi + b.mu * (1.0 - Phi) + delta * phi;
    double e_z2 = (a.var + a.mu * a.mu) * Phi + (b.var + b.mu * b.mu) * (1.0 - Phi) + (a.mu + b.mu) * delta * phi;
    double var_Z = std::max(0.0, e_z2 - mu_Z * mu_Z);

    return {mu_Z, var_Z};
}

static GaussianMoments min_gaussian_moments(GaussianMoments a, GaussianMoments b) {
    g_moment_stats.num_min_calls_total++;
    // min(X, Y) = -max(-X, -Y)
    GaussianMoments neg_a = {-a.mu, a.var};
    GaussianMoments neg_b = {-b.mu, b.var};
    GaussianMoments res = max_gaussian_moments(neg_a, neg_b);
    return {-res.mu, res.var};
}

void update_physical_state(const FactorGraphView& fg, 
                      PhysicalState& phys_state, 
                      const ProbTimingConfig& config,
                      const vtr::vector_map<ClusterBlockId, t_block_loc>& block_locs,
                      const NetCostHandler* net_cost_handler) {
    if (config.mode != UncertaintyMode::PHYSICAL_COMBINED && config.beta <= 1e-9 && config.gamma <= 1e-9 && config.gamma_b <= 1e-9) {
        return;
    }

    auto& atom_ctx = g_vpr_ctx.atom();

    // Resize if needed (sparse mapping)
    if (phys_state.edge_phys_scales.size() != fg.edge_src.size()) {
        phys_state.edge_phys_scales.assign(fg.edge_src.size(), 1.0f);
    }
    // [Option B] Resize routing penalty vector
    if (phys_state.edge_routing_penalty.size() != fg.edge_src.size()) {
        phys_state.edge_routing_penalty.assign(fg.edge_src.size(), 0.0f);
    }
    
    if (block_locs.empty()) return;

    // [PHASE 7] Congestion Map Access
    const auto* chan_util = (net_cost_handler) ? &(net_cost_handler->get_chan_util()) : nullptr;

    size_t edges_updated = 0;
    
    for (size_t e_idx = 0; e_idx < fg.edge_src.size(); ++e_idx) {
        if (!fg.is_interconnect_edge[e_idx]) continue;

        tatum::NodeId src_node = fg.edge_src[e_idx];
        tatum::NodeId dst_node = fg.edge_dst[e_idx]; 

        // 1. Resolve Source Location
        AtomPinId src_pin = atom_ctx.lookup().tnode_atom_pin(src_node);
        if (!src_pin) continue;
        AtomBlockId src_blk = atom_ctx.netlist().pin_block(src_pin);
        if (!src_blk) continue;
        ClusterBlockId src_clb = atom_ctx.lookup().atom_clb(src_blk);
        if (src_clb == ClusterBlockId::INVALID() || size_t(src_clb) >= block_locs.size()) continue;

        // 2. Resolve Dest Location
        AtomPinId dst_pin = atom_ctx.lookup().tnode_atom_pin(dst_node);
        if (!dst_pin) continue;
        AtomBlockId dst_blk = atom_ctx.netlist().pin_block(dst_pin);
        if (!dst_blk) continue;
        ClusterBlockId dst_clb = atom_ctx.lookup().atom_clb(dst_blk);
        if (dst_clb == ClusterBlockId::INVALID() || size_t(dst_clb) >= block_locs.size()) continue;

        const auto& src_loc = block_locs[src_clb];
        const auto& dst_loc = block_locs[dst_clb];

        int dx = std::abs(src_loc.loc.x - dst_loc.loc.x);
        int dy = std::abs(src_loc.loc.y - dst_loc.loc.y);
        int dist = dx + dy;

        // 3. Physical Factor: Base Distance Scaling
        float scale = 1.0f + config.beta * (float)dist;

        // 3b. Fanout-Aware Variance Scaling
        if (config.beta2 > 1e-9f) {
            AtomNetId src_net = atom_ctx.netlist().pin_net(src_pin);
            if (src_net) {
                size_t fanout = atom_ctx.netlist().net_sinks(src_net).size();
                if (fanout > 1) {
                    scale *= (1.0f + config.beta2 * std::log((float)fanout));
                }
            }
        }

        // 3c. Channel Capacity Variance Scaling
        // Low routing capacity at edge location = more uncertainty about routed delay
        // x_list[y] = horizontal channel width at row y
        // y_list[x] = vertical channel width at column x
        if (config.beta3 > 1e-9f) {
            const auto& cw = g_vpr_ctx.device().chan_width;
            int mid_x = (src_loc.loc.x + dst_loc.loc.x) / 2;
            int mid_y = (src_loc.loc.y + dst_loc.loc.y) / 2;

            int cap_x = (mid_y >= 0 && mid_y < (int)cw.x_list.size()) ? cw.x_list[mid_y] : cw.x_max;
            int cap_y = (mid_x >= 0 && mid_x < (int)cw.y_list.size()) ? cw.y_list[mid_x] : cw.y_max;
            float avg_cap = (float)std::max(cap_x + cap_y, 2) / 2.0f;
            scale *= (1.0f + config.beta3 / avg_cap);
        }

        // 4. [PHASE 7] Congestion-Aware Inflation
        if (chan_util && config.gamma > 0.0f && !(*chan_util).x.empty() && !(*chan_util).y.empty()) {
            auto src_l = src_loc.loc.layer;
            auto src_x = src_loc.loc.x;
            auto src_y = src_loc.loc.y;
            auto dst_l = dst_loc.loc.layer;
            auto dst_x = dst_loc.loc.x;
            auto dst_y = dst_loc.loc.y;

            if (src_l < (*chan_util).x.dim_size(0) && src_x < (*chan_util).x.dim_size(1) && src_y < (*chan_util).x.dim_size(2) &&
                dst_l < (*chan_util).x.dim_size(0) && dst_x < (*chan_util).x.dim_size(1) && dst_y < (*chan_util).x.dim_size(2)) {
                
                double c_src_x = (*chan_util).x[src_l][src_x][src_y];
                double c_src_y = (*chan_util).y[src_l][src_x][src_y];
                double c_dst_x = (*chan_util).x[dst_l][dst_x][dst_y];
                double c_dst_y = (*chan_util).y[dst_l][dst_x][dst_y];
                
                float avg_cong = (float)(c_src_x + c_src_y + c_dst_x + c_dst_y) / 4.0f;
                scale *= (1.0f + config.gamma * avg_cong);

                // [Option B] Routing penalty: shift mean delay upward in congested areas
                if (config.gamma_b > 1e-9f && e_idx < phys_state.edge_routing_penalty.size()) {
                    phys_state.edge_routing_penalty[e_idx] = config.gamma_b * avg_cong;
                }

            }
        }

        if (e_idx < phys_state.edge_phys_scales.size()) {
            phys_state.edge_phys_scales[e_idx] = scale;
            edges_updated++;
        }
    }
    
    VTR_LOG("INSTRUMENTATION: Physical State Updated. Edges Scaled: %zu\n", edges_updated);
}

double FactorGraphView::get_quantile_slack95(float q) const {
    std::vector<double> s95_values;
    s95_values.reserve(topo_nodes.size());

    for (size_t n_idx = 0; n_idx < mu_var_S.size(); ++n_idx) {
        GaussianMoments S = mu_var_S[n_idx];
        if (S.is_set()) {
            double p_mu = S.mu;
            double p_std = std::sqrt(std::max(0.0, S.var));
            double p_slack95 = p_mu - 1.64485 * p_std;
            s95_values.push_back(p_slack95);
        }
    }

    if (s95_values.empty()) return 1e20;

    std::sort(s95_values.begin(), s95_values.end());
    size_t idx = (size_t)((float)(s95_values.size() - 1) * q);
    return s95_values[idx];
}

// Removed Mode 3 max_gaussian_mode3


size_t FactorGraphView::get_structural_hash() const {
    size_t hash = 0;
    auto combine = [&](size_t val) {
        hash ^= val + 0x9e3779b9 + (hash << 6) + (hash >> 2);
    };

    combine(num_nodes);
    combine(num_edges);
    for (auto node_id : topo_nodes) combine(size_t(node_id));
    for (size_t i = 0; i < in_edges.size(); ++i) {
        combine(i);
        for (auto e : in_edges[i]) combine(size_t(e));
    }
    for (size_t i = 0; i < out_edges.size(); ++i) {
        combine(i);
        for (auto e : out_edges[i]) combine(size_t(e));
    }
    for (auto cid : candidate_id_for_edge) combine(cid);
    
    return hash;
}

FactorGraphView build_factor_graph_view(const tatum::TimingGraph& tg) {
    VTR_LOG("INSTRUMENTATION: Building FactorGraphView...\n");

    FactorGraphView view;
    view.num_nodes = tg.nodes().size();
    view.num_edges = tg.edges().size();

    size_t max_node_id = 0;
    for (auto node_id : tg.nodes()) max_node_id = std::max(max_node_id, size_t(node_id));
    size_t max_edge_id = 0;
    for (auto edge_id : tg.edges()) max_edge_id = std::max(max_edge_id, size_t(edge_id));

    size_t node_storage_size = max_node_id + 1;
    size_t edge_storage_size = max_edge_id + 1;

    view.in_edges.resize(node_storage_size);
    view.out_edges.resize(node_storage_size);
    view.candidate_ids_into_node.resize(node_storage_size);
    view.edge_src.resize(edge_storage_size);
    view.edge_dst.resize(edge_storage_size);
    view.candidate_id_for_edge.resize(edge_storage_size);
    view.is_interconnect_edge.resize(edge_storage_size);
    view.node_levels.resize(node_storage_size, 0);

    // Pre-allocate moment buffers
    view.mu_var_A.resize(node_storage_size);
    view.mu_var_R.resize(node_storage_size);
    view.mu_var_B.resize(edge_storage_size);
    view.mu_var_S.resize(node_storage_size);

    for (auto level_id : tg.levels()) {
        for (auto node_id : tg.level_nodes(level_id)) {
            view.topo_nodes.push_back(node_id);
            view.node_levels[size_t(node_id)] = (int)size_t(level_id);
        }
    }

    long n_interconnect = 0;
    long n_prim_comb = 0;
    long n_prim_clock = 0;
    long n_other = 0;

    int running_candidate_id = 0;
    for (auto edge_id : tg.edges()) {
        auto type = tg.edge_type(edge_id);
        if (type == tatum::EdgeType::INTERCONNECT) n_interconnect++;
        else if (type == tatum::EdgeType::PRIMITIVE_COMBINATIONAL) n_prim_comb++;
        else if (type == tatum::EdgeType::PRIMITIVE_CLOCK_LAUNCH) n_prim_clock++;
        else n_other++;

        tatum::NodeId src = tg.edge_src_node(edge_id);
        tatum::NodeId dst = tg.edge_sink_node(edge_id);
        size_t e_idx = size_t(edge_id);
        view.edge_src[e_idx] = src;
        view.edge_dst[e_idx] = dst;
        view.in_edges[size_t(dst)].push_back(edge_id);
        view.out_edges[size_t(src)].push_back(edge_id);
        view.candidate_id_for_edge[e_idx] = running_candidate_id++;
        view.is_interconnect_edge[e_idx] = (tg.edge_type(edge_id) == tatum::EdgeType::INTERCONNECT);
    }

    for (auto node_id : tg.nodes()) {
        size_t n_idx = size_t(node_id);
        size_t fanin = view.in_edges[n_idx].size();
        if (fanin > 1) view.reconvergent_node_count++;
        int hist_bucket = (fanin >= 6) ? 6 : (int)fanin;
        view.fanin_histogram[hist_bucket]++;
        for (auto edge_id : view.in_edges[n_idx]) {
            view.candidate_ids_into_node[n_idx].push_back(view.candidate_id_for_edge[size_t(edge_id)]);
        }
    }

    // [PHASE 7] Calculate Structural Self-Calibration Metrics
    if (view.num_nodes > 0) {
        view.reconvergence_density = (float)view.reconvergent_node_count / (float)view.num_nodes;
        
        // Find Median Logic Depth
        std::vector<int> levels = view.node_levels;
        std::sort(levels.begin(), levels.end());
        if (!levels.empty()) {
            view.median_logic_depth = (float)levels[levels.size() / 2];
        }
    }

    VTR_LOG("INSTRUMENTATION: FactorGraphView Summary: Nodes=%zu, Edges=%zu, ReconvDensity=%.3f, MedianDepth=%.1f\n", 
           view.num_nodes, view.num_edges, view.reconvergence_density, view.median_logic_depth);
    VTR_LOG("INSTRUMENTATION: Hash=%zu\n", view.get_structural_hash());
    VTR_LOG("INSTRUMENTATION: Edge Types: Interconnect=%ld, PrimComb=%ld, PrimClock=%ld, Other=%ld\n",
            n_interconnect, n_prim_comb, n_prim_clock, n_other);
    return view;
}


ProbTimingSummary run_probabilistic_timing(FactorGraphView& fg, 
                                 const tatum::TimingGraph& tg,
                                 const tatum::SetupTimingAnalyzer& analyzer,
                                 const tatum::DelayCalculator& delay_calc,
                                 const PhysicalState& phys_state,
                                 const ProbTimingConfig& config) {
    
    // --- PROB_TIMING_READINESS Check (Once) ---
    static bool readiness_logged = false;
    if (!readiness_logged) {
        VTR_LOG("\nPROB_TIMING_READINESS:\n");
        VTR_LOG("  Modes implemented: 0 1 4\n");
        VTR_LOG("  Topology hash: %zu\n", fg.get_structural_hash());
        VTR_LOG("  Injection enabled: false\n");
        VTR_LOG("  Forced binning: %s\n", config.forced_binning ? "true" : "false");
        VTR_LOG("  Timing API stable: true\n\n");
        readiness_logged = true;
    }

    auto start_time = std::chrono::high_resolution_clock::now();
    VTR_LOG("INSTRUMENTATION: Starting Mode %d Analysis. Hash=%zu\n", (int)config.mode, fg.get_structural_hash());



    // Removed Mode 3 sensitivity buffer reset


    std::vector<double> tmp_sens(phys_state.edge_phys_scales.size(), 0.0);

    // 1. Forward Pass (Arrival)
    for (auto node_id : fg.topo_nodes) {
        if (size_t(node_id) >= fg.in_edges.size()) continue;
        size_t n_idx = size_t(node_id);
        GaussianMoments current_A = GaussianMoments::unset();
        
        auto tags = analyzer.setup_tags(node_id, tatum::TagType::DATA_ARRIVAL);
        if (!tags.empty()) {
            auto max_tag = tatum::find_maximum_tag(tags);
            current_A = {max_tag->time().value(), 0.0};
        }

        for (auto edge_id : fg.in_edges[n_idx]) {
            size_t e_idx = size_t(edge_id);
            tatum::NodeId src = fg.edge_src[e_idx];
            if (size_t(src) >= fg.mu_var_A.size()) continue;
            GaussianMoments src_A = fg.mu_var_A[size_t(src)];
            
            auto edge_type = tg.edge_type(edge_id);
            if (src_A.is_set() && 
                (edge_type == tatum::EdgeType::INTERCONNECT || 
                 edge_type == tatum::EdgeType::PRIMITIVE_COMBINATIONAL || 
                 edge_type == tatum::EdgeType::PRIMITIVE_CLOCK_LAUNCH)) {
                
                double mu_D = delay_calc.max_edge_delay(tg, edge_id).value();
                double var_D = std::pow(config.alpha * mu_D, 2.0);

                if (config.mode == UncertaintyMode::PHYSICAL_COMBINED &&
                    !phys_state.edge_phys_scales.empty() &&
                    e_idx < phys_state.edge_phys_scales.size()) {

                    float scale = phys_state.edge_phys_scales[e_idx];
                    var_D *= scale; // Scale variance by physical factor
                }

                // [Option B] Apply routing penalty to mean delay
                // Congested edges are expected to have higher routed delay
                if (!phys_state.edge_routing_penalty.empty() &&
                    e_idx < phys_state.edge_routing_penalty.size()) {
                    float penalty = phys_state.edge_routing_penalty[e_idx];
                    if (penalty > 1e-9f) {
                        mu_D *= (1.0 + (double)penalty); // Shift mean upward
                    }
                }

                GaussianMoments candidate_B = {src_A.mu + mu_D, src_A.var + var_D};
                if (e_idx < fg.mu_var_B.size()) {
                    fg.mu_var_B[e_idx] = candidate_B;
                }

                current_A = max_gaussian_moments(current_A, candidate_B);
            }
        }
        fg.mu_var_A[n_idx] = current_A;

        // [PHASE 7.1] Proactive Slack-Gating
        if (config.slack_gate > 0.0f) {
            auto slacks = analyzer.setup_slacks(node_id);
            if (!slacks.empty()) {
                float s = tatum::find_minimum_tag(slacks)->time().value();
                // Relative Gate: Mask if slack is significantly better than worst slack
                if (s > config.min_slack + config.slack_gate) {
                    fg.mu_var_A[n_idx].var = 0.0; // Mask this node
                }
            }
        }
    }

    // 2. Backward Pass (Required)
    for (size_t i = fg.topo_nodes.size(); i > 0; --i) {
        tatum::NodeId node_id = fg.topo_nodes[i-1];
        if (size_t(node_id) >= fg.out_edges.size()) continue;
        size_t n_idx = size_t(node_id);
        GaussianMoments current_R = GaussianMoments::unset();

        auto tags = analyzer.setup_tags(node_id, tatum::TagType::DATA_REQUIRED);
        if (!tags.empty()) {
            auto min_tag = tatum::find_minimum_tag(tags);
            current_R = {min_tag->time().value(), 0.0};
        }

        for (auto edge_id : fg.out_edges[n_idx]) {
            size_t e_idx = size_t(edge_id);
            tatum::NodeId dst = fg.edge_dst[e_idx];
            if (size_t(dst) >= fg.mu_var_R.size()) continue;
            GaussianMoments dst_R = fg.mu_var_R[size_t(dst)];

            auto edge_type = tg.edge_type(edge_id);
            if (dst_R.is_set() && 
                (edge_type == tatum::EdgeType::INTERCONNECT || 
                 edge_type == tatum::EdgeType::PRIMITIVE_COMBINATIONAL || 
                 edge_type == tatum::EdgeType::PRIMITIVE_CLOCK_LAUNCH)) {
                
                double mu_D = delay_calc.max_edge_delay(tg, edge_id).value();
                double var_D = std::pow(config.alpha * mu_D, 2.0);
                
                if (config.mode == UncertaintyMode::PHYSICAL_COMBINED && 
                    !phys_state.edge_phys_scales.empty() && 
                    e_idx < phys_state.edge_phys_scales.size()) {
                    
                    float scale = phys_state.edge_phys_scales[e_idx];
                    var_D *= scale;
                }

                GaussianMoments candidate_R = {dst_R.mu - mu_D, dst_R.var + var_D};
                current_R = min_gaussian_moments(current_R, candidate_R);
            }
        }
        fg.mu_var_R[n_idx] = current_R;

        // [PHASE 7.1] Proactive Slack-Gating (Backward)
        if (config.slack_gate > 0.0f) {
            auto slacks = analyzer.setup_slacks(node_id);
            if (!slacks.empty()) {
                float s = tatum::find_minimum_tag(slacks)->time().value();
                if (s > config.slack_gate) {
                    fg.mu_var_R[n_idx].var = 0.0; // Mask this node
                }
            }
        }
    }

    // 3. Slack & WorstSlack95
    double worst_S95 = std::numeric_limits<double>::infinity();
    int endpoint_count = 0;
    tatum::NodeId worst_node = tatum::NodeId::INVALID();

    for (auto node_id : tg.nodes()) {
        size_t n_idx = size_t(node_id);
        auto t_slacks = analyzer.setup_slacks(node_id);
        if (!t_slacks.empty() && n_idx < fg.mu_var_A.size() && n_idx < fg.mu_var_R.size()) {
            GaussianMoments A = fg.mu_var_A[n_idx];
            GaussianMoments R = fg.mu_var_R[n_idx];
            if (A.is_set() && R.is_set()) {
                double mu_S = R.mu - A.mu;
                double var_S = R.var + A.var;
                double S95 = mu_S - 1.64485 * std::sqrt(std::max(0.0, var_S));
                if (n_idx < fg.mu_var_S.size()) {
                    fg.mu_var_S[n_idx] = {mu_S, var_S};
                }
                
                if (S95 < worst_S95) {
                    worst_S95 = S95;
                    worst_node = node_id;
                }
                endpoint_count++;
            }
        }
    }

    auto end_time = std::chrono::high_resolution_clock::now();
    double duration = std::chrono::duration_cast<std::chrono::microseconds>(end_time - start_time).count() / 1000.0;

    VTR_LOG("INSTRUMENTATION: Probabilistic Timing Analysis Result:\n");
    VTR_LOG("  Mode: %d, SlackGate: %.3f ns, WorstSlack95: %.3g (seconds)\n", 
            (int)config.mode, config.slack_gate * 1e9, worst_S95);
    VTR_LOG("  Runtime: %.2f ms, Endpoints: %d\n", duration, endpoint_count);

    // Mode 3 logging removed


    ProbTimingSummary summary;
    summary.worst_slack_95 = worst_S95;
    summary.num_endpoints = endpoint_count;
    summary.worst_endpoint_node_id = worst_node;
    summary.runtime_ms = duration;
    // Let's count weighted vs empty from bin_state
    size_t n_weighted = 0;
    size_t n_empty = 0;
    size_t n_inter = 0;
    if (config.mode == UncertaintyMode::PHYSICAL_COMBINED) {
        for(size_t i=0; i<fg.is_interconnect_edge.size(); ++i) {
            if (fg.is_interconnect_edge[i]) {
                n_inter++;
                if (i < phys_state.edge_phys_scales.size() && phys_state.edge_phys_scales[i] > 1.0f) n_weighted++; // Scaled > 1.0
                else n_empty++;
            }
        }
    }
    summary.num_interconnect_edges = n_inter;
    summary.num_weighted_edges = n_weighted;
    summary.num_empty_weight_edges = n_empty;
    summary.moments = g_moment_stats;

    return summary;
}
