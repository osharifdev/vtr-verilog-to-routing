#include "FactorGraphView.h"
#include "tatum/TimingGraph.hpp"
#include "tatum/analyzers/SetupTimingAnalyzer.hpp"
#include "tatum/delay_calc/DelayCalculator.hpp"
#include "vtr_log.h"
#include "globals.h"
#include "atom_netlist.h"
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

static GaussianMoments max_gaussian_moments(GaussianMoments a, GaussianMoments b) {
    if (!a.is_set()) return b;
    if (!b.is_set()) return a;

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
    // min(X, Y) = -max(-X, -Y)
    GaussianMoments neg_a = {-a.mu, a.var};
    GaussianMoments neg_b = {-b.mu, b.var};
    GaussianMoments res = max_gaussian_moments(neg_a, neg_b);
    return {-res.mu, res.var};
}

void update_bin_state(const FactorGraphView& fg, BinState& bin_state, const ProbTimingConfig& config) {
    if (config.mode == UncertaintyMode::DETERMINISTIC || config.mode == UncertaintyMode::INDEPENDENT) {
        return;
    }

    bin_state.bins_x = config.bins_x;
    bin_state.bins_y = config.bins_y;
    bin_state.num_bins = config.bins_x * config.bins_y;
    
    bin_state.bin_costs.assign(bin_state.num_bins, 0.0);
    bin_state.edge_bin_weights.assign(fg.edge_src.size(), {});
    
    // --- VALIDATION HARNESS (Gated) ---
    if (config.forced_binning) {
        size_t edges_with_weights = 0;
        for (size_t e_idx = 0; e_idx < fg.edge_src.size(); ++e_idx) {
            if (!fg.is_interconnect_edge[e_idx]) continue;
            int bin_id = 0; 
            float weight = 1.0f;
            bin_state.edge_bin_weights[e_idx].push_back({bin_id, weight});
            bin_state.bin_costs[bin_id] += weight;
            edges_with_weights++;
        }
        VTR_LOG("INSTRUMENTATION: [Forced Binning ON] All interconnect edges mapped to Bin 0. Edges=%zu\n", edges_with_weights);
        return;
    }

    // --- PRODUCTION LOGIC ---
    const auto& place_ctx = g_vpr_ctx.placement();
    const auto& device_ctx = g_vpr_ctx.device();
    const auto& atom_ctx = g_vpr_ctx.atom();
    
    // Safety: If placement hasn't started (block_locs empty), we can't map to bins.
    // Fallback: Leave weights empty (Mode 3 will degrade to Mode 1/0 gracefully).
    if (place_ctx.block_locs().empty()) {
        VTR_LOG_WARN("Probabilistic Timing: Placement context empty. Skipping bin mapping.\n");
        return;
    }

    int nx = device_ctx.grid.width();
    int ny = device_ctx.grid.height();
    if (nx <= 0 || ny <= 0) return;

    double bin_w = (double)nx / (double)config.bins_x;
    double bin_h = (double)ny / (double)config.bins_y;
    
    size_t edges_with_weights = 0;
    size_t total_interconnect = 0;

    for (size_t e_idx = 0; e_idx < fg.edge_src.size(); ++e_idx) {
        if (!fg.is_interconnect_edge[e_idx]) continue;
        total_interconnect++;

        tatum::NodeId src_node = fg.edge_src[e_idx];

        // Resolve Placement Location
        // We need the AtomBlockId -> ClusterBlockId -> Location
        AtomPinId atom_pin = atom_ctx.lookup().tnode_atom_pin(src_node);
        if (!atom_pin) continue; // Not an atom pin (e.g. virtual sources)

        AtomBlockId atom_blk = atom_ctx.netlist().pin_block(atom_pin);
        if (!atom_blk) continue;

        ClusterBlockId clb_blk = atom_ctx.lookup().atom_clb(atom_blk);
        if (clb_blk == ClusterBlockId::INVALID()) continue;

        if (size_t(clb_blk) >= place_ctx.block_locs().size()) continue;

        const auto& block_loc = place_ctx.block_locs()[clb_blk];
        int x = block_loc.loc.x;
        int y = block_loc.loc.y;

        // Map to Bin
        int bx = std::min(config.bins_x - 1, std::max(0, (int)(x / bin_w)));
        int by = std::min(config.bins_y - 1, std::max(0, (int)(y / bin_h)));
        int bin_id = by * config.bins_x + bx;

        // Assign Weight (currently 1.0 to single spatial bin)
        float weight = 1.0f;
        bin_state.edge_bin_weights[e_idx].push_back({bin_id, weight});
        if (bin_id >= (int)bin_state.bin_costs.size()) continue;
        
        bin_state.bin_costs[bin_id] += weight; // Cost = congestion/count
        edges_with_weights++;
    }
    
    VTR_LOG("INSTRUMENTATION: Bin State Updated. Interconnect Edges: %zu, Weighted: %zu (%.1f%%)\n", 
            total_interconnect, edges_with_weights, 
            (total_interconnect > 0) ? 100.0 * edges_with_weights / total_interconnect : 0.0);
}

static GaussianMoments max_gaussian_mode3(GaussianMoments a, const std::vector<double>& a_sens,
                                          GaussianMoments b, const std::vector<double>& b_sens,
                                          double gamma, const std::vector<double>& bin_costs,
                                          std::vector<double>& res_sens, double& last_rho) {
    if (!a.is_set()) { res_sens = b_sens; return b; }
    if (!b.is_set()) { res_sens = a_sens; return a; }

    double cov_ab = 0.0;
    for (size_t i = 0; i < a_sens.size(); ++i) {
        if (a_sens[i] != 0.0 && b_sens[i] != 0.0) {
            cov_ab += a_sens[i] * b_sens[i] * bin_costs[i];
        }
    }
    cov_ab *= gamma;

    double var_diff = a.var + b.var - 2.0 * cov_ab;
    double delta = std::sqrt(std::max(1e-24, var_diff));

    double rho = cov_ab / (std::sqrt(std::max(1e-24, a.var)) * std::sqrt(std::max(1e-24, b.var)));
    last_rho = std::max(-1.0, std::min(1.0, rho));

    if (delta < 1e-12) {
        if (a.mu > b.mu) { res_sens = a_sens; return a; }
        else { res_sens = b_sens; return b; }
    }

    double alpha = (a.mu - b.mu) / delta;
    if (alpha > 8.0) { res_sens = a_sens; return a; }
    if (alpha < -8.0) { res_sens = b_sens; return b; }

    double Phi = normal_cdf(alpha);
    double phi = normal_pdf(alpha);

    double mu_Z = a.mu * Phi + b.mu * (1.0 - Phi) + delta * phi;
    double e_z2 = (a.var + a.mu * a.mu) * Phi + (b.var + b.mu * b.mu) * (1.0 - Phi) + (a.mu + b.mu) * delta * phi;
    double var_Z = std::max(0.0, e_z2 - mu_Z * mu_Z);

    for (size_t i = 0; i < a_sens.size(); ++i) {
        res_sens[i] = Phi * a_sens[i] + (1.0 - Phi) * b_sens[i];
    }

    return {mu_Z, var_Z};
}

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

    // Pre-allocate moment buffers
    view.mu_var_A.resize(node_storage_size);
    view.mu_var_R.resize(node_storage_size);
    view.mu_var_B.resize(edge_storage_size);
    view.mu_var_S.resize(node_storage_size);

    for (auto level_id : tg.levels()) {
        for (auto node_id : tg.level_nodes(level_id)) {
            view.topo_nodes.push_back(node_id);
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

    VTR_LOG("INSTRUMENTATION: FactorGraphView Summary: Nodes=%zu, Edges=%zu, Hash=%zu\n", 
           view.num_nodes, view.num_edges, view.get_structural_hash());
    VTR_LOG("INSTRUMENTATION: Edge Types: Interconnect=%ld, PrimComb=%ld, PrimClock=%ld, Other=%ld\n",
            n_interconnect, n_prim_comb, n_prim_clock, n_other);
    return view;
}


ProbTimingSummary run_probabilistic_timing(FactorGraphView& fg, 
                                 const tatum::TimingGraph& tg,
                                 const tatum::SetupTimingAnalyzer& analyzer,
                                 const tatum::DelayCalculator& delay_calc,
                                 const BinState& bin_state,
                                 const ProbTimingConfig& config) {
    
    // --- PROB_TIMING_READINESS Check (Once) ---
    static bool readiness_logged = false;
    if (!readiness_logged) {
        VTR_LOG("\nPROB_TIMING_READINESS:\n");
        VTR_LOG("  Modes implemented: 0 1 2 3\n");
        VTR_LOG("  Topology hash: %zu\n", fg.get_structural_hash());
        VTR_LOG("  Injection enabled: false\n");
        VTR_LOG("  Forced binning: %s\n", config.forced_binning ? "true" : "false");
        VTR_LOG("  Timing API stable: true\n\n");
        readiness_logged = true;
    }

    auto start_time = std::chrono::high_resolution_clock::now();
    VTR_LOG("INSTRUMENTATION: Starting Mode %d Analysis. Hash=%zu\n", (int)config.mode, fg.get_structural_hash());

    // Initialize sensitivity vectors for Mode 3 - FIX FOR CRASH
    if (config.mode == UncertaintyMode::BIN_LATENT_CORR) {
        int num_bins = std::max(1, config.bins_x * config.bins_y); 
        if (fg.mu_var_A_sens.empty() || fg.mu_var_A_sens[0].size() != size_t(num_bins)) {
             fg.mu_var_A_sens.assign(fg.mu_var_A.size(), std::vector<double>(num_bins, 0.0));
        }
    }

    // Exposure Logging (Correlation Table)
    if (config.mode == UncertaintyMode::BIN_LATENT_CORR && config.alpha == 0.0f && config.gamma > 1e-9) {
        VTR_LOG("INSTRUMENTATION: Mode 3 Exposure - Pairwise Correlation Table\n");
        VTR_LOG("Edge1\tEdge2\tSharedBins\tVar(D1)\tVar(D2)\tCov(D1,D2)\tRho\n");
        
        if (bin_state.edge_bin_weights.size() != fg.edge_src.size()) {
             VTR_LOG("ERROR: BinState not synced with FactorGraph! Skipping exposure table.\n");
        } else {
            int shared_count = 0;
            int distant_count = 0;
            for (size_t i = 0; i < fg.edge_src.size() && (shared_count < 5 || distant_count < 5); i += 50) {
                if (!fg.is_interconnect_edge[i]) {
                     // VTR_LOG("Skip i=%zu: Not Interconnect\n", i);
                     continue;
                }
                const auto& w1 = bin_state.edge_bin_weights[i];
                if (w1.empty()) {
                     VTR_LOG("Skip i=%zu: Empty Weights\n", i);
                     continue;
                }

                for (size_t j = i + 1; j < fg.edge_src.size(); j += 50) {
                    if (!fg.is_interconnect_edge[j]) continue;
                    if (j >= bin_state.edge_bin_weights.size()) continue;
                    const auto& w2 = bin_state.edge_bin_weights[j];
                    if (w2.empty()) continue;

                    double cov = 0.0;
                    double w_intersect = 0.0; 
                    for (const auto& b1 : w1) {
                        for (const auto& b2 : w2) {
                            if (b1.first == b2.first) {
                                if (size_t(b1.first) < bin_state.bin_costs.size()) {
                                    cov += b1.second * b2.second * bin_state.bin_costs[b1.first];
                                    w_intersect += 1.0;
                                }
                            }
                        }
                    }
                    cov *= config.gamma;
                    
                    double var1 = 0.0; 
                    for (const auto& b : w1) var1 += b.second * b.second * bin_state.bin_costs[b.first];
                    var1 *= config.gamma; 
                    
                    double var2 = 0.0;
                    for (const auto& b : w2) var2 += b.second * b.second * bin_state.bin_costs[b.first];
                    var2 *= config.gamma; 

                    double rho = (var1 > 0 && var2 > 0) ? cov / std::sqrt(var1 * var2) : 0.0;
                    
                    if (w_intersect > 0 && shared_count < 5) {
                        VTR_LOG("%zu\t%zu\tYes\t%.3g\t%.3g\t%.3g\t%.3f\n", i, j, var1, var2, cov, rho);
                        shared_count++;
                    } else if (w_intersect == 0 && distant_count < 5) {
                         VTR_LOG("%zu\t%zu\tNo\t%.3g\t%.3g\t%.3g\t%.3f\n", i, j, var1, var2, cov, rho);
                         distant_count++;
                    }
                    
                    if (shared_count >= 5 && distant_count >= 5) break; 
                }
            }
        }
    }

    if (config.mode == UncertaintyMode::BIN_LATENT_CORR) {
        // Prepare sensitivity buffer (reset)
        for(auto& vec : fg.mu_var_A_sens) {
            std::fill(vec.begin(), vec.end(), 0.0);
        }
    }

    double sample_rho = 0.0;
    std::vector<double> tmp_sens(bin_state.num_bins, 0.0);
    bool reconvergence_logged = false;

    // 1. Forward Pass (Arrival)
    for (auto node_id : fg.topo_nodes) {
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
            GaussianMoments src_A = fg.mu_var_A[size_t(src)];
            
            auto edge_type = tg.edge_type(edge_id);
            if (src_A.is_set() && 
                (edge_type == tatum::EdgeType::INTERCONNECT || 
                 edge_type == tatum::EdgeType::PRIMITIVE_COMBINATIONAL || 
                 edge_type == tatum::EdgeType::PRIMITIVE_CLOCK_LAUNCH)) {
                
                double mu_D = delay_calc.max_edge_delay(tg, edge_id).value();
                double var_D = std::pow(config.alpha * mu_D, 2.0);
                
                if (config.mode >= UncertaintyMode::BIN_AWARE_VAR && !bin_state.edge_bin_weights[e_idx].empty()) {
                    for (const auto& bw : bin_state.edge_bin_weights[e_idx]) {
                        var_D += (bw.second * bw.second) * config.gamma * bin_state.bin_costs[bw.first];
                    }
                }

                GaussianMoments candidate_B = {src_A.mu + mu_D, src_A.var + var_D};
                fg.mu_var_B[e_idx] = candidate_B;

                if (config.mode == UncertaintyMode::BIN_LATENT_CORR) {
                    tmp_sens = fg.mu_var_A_sens[size_t(src)];
                    for (const auto& bw : bin_state.edge_bin_weights[e_idx]) {
                        tmp_sens[bw.first] += bw.second; 
                    }
                    
                    double rho = 0.0;
                    current_A = max_gaussian_mode3(current_A, fg.mu_var_A_sens[n_idx], 
                                                 candidate_B, tmp_sens, 
                                                 config.gamma, bin_state.bin_costs, 
                                                 fg.mu_var_A_sens[n_idx], rho);
                    
                    if (config.alpha == 0.0f && std::abs(rho) > 0.01 && !reconvergence_logged) {
                        VTR_LOG("INSTRUMENTATION: Reconvergence Log - Node %zu, Rho=%.3f, Result Var=%.3g\n", 
                                size_t(node_id), rho, current_A.var);
                         reconvergence_logged = true;
                    }
                    sample_rho = rho;
                } else {
                    current_A = max_gaussian_moments(current_A, candidate_B);
                }
            }
        }
        fg.mu_var_A[n_idx] = current_A;
    }

    // 2. Backward Pass (Required)
    for (size_t i = fg.topo_nodes.size(); i > 0; --i) {
        tatum::NodeId node_id = fg.topo_nodes[i-1];
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
            GaussianMoments dst_R = fg.mu_var_R[size_t(dst)];

            auto edge_type = tg.edge_type(edge_id);
            if (dst_R.is_set() && 
                (edge_type == tatum::EdgeType::INTERCONNECT || 
                 edge_type == tatum::EdgeType::PRIMITIVE_COMBINATIONAL || 
                 edge_type == tatum::EdgeType::PRIMITIVE_CLOCK_LAUNCH)) {
                
                double mu_D = delay_calc.max_edge_delay(tg, edge_id).value();
                double var_D = std::pow(config.alpha * mu_D, 2.0);
                
                if (config.mode >= UncertaintyMode::BIN_AWARE_VAR && !bin_state.edge_bin_weights[e_idx].empty()) {
                     for (const auto& bw : bin_state.edge_bin_weights[e_idx]) {
                        var_D += (bw.second * bw.second) * config.gamma * bin_state.bin_costs[bw.first];
                    }
                }

                GaussianMoments candidate_R = {dst_R.mu - mu_D, dst_R.var + var_D};
                current_R = min_gaussian_moments(current_R, candidate_R);
            }
        }
        fg.mu_var_R[n_idx] = current_R;
    }

    // 3. Slack & WorstSlack95
    double worst_S95 = std::numeric_limits<double>::infinity();
    int endpoint_count = 0;
    tatum::NodeId worst_node = tatum::NodeId::INVALID();

    for (auto node_id : tg.nodes()) {
        size_t n_idx = size_t(node_id);
        auto t_slacks = analyzer.setup_slacks(node_id);
        if (!t_slacks.empty()) {
            GaussianMoments A = fg.mu_var_A[n_idx];
            GaussianMoments R = fg.mu_var_R[n_idx];
            if (A.is_set() && R.is_set()) {
                double mu_S = R.mu - A.mu;
                double var_S = R.var + A.var;
                double S95 = mu_S - 1.64485 * std::sqrt(std::max(0.0, var_S));
                fg.mu_var_S[n_idx] = {mu_S, var_S};
                
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
    VTR_LOG("  Mode: %d, WorstSlack95: %.3g (seconds)\n", (int)config.mode, worst_S95);
    VTR_LOG("  Runtime: %.2f ms, Endpoints: %d\n", duration, endpoint_count);

    if (config.mode == UncertaintyMode::BIN_LATENT_CORR) {
        VTR_LOG("  Sample Correlation (rho): %.3f\n", sample_rho);
    }

    ProbTimingSummary summary;
    summary.worst_slack_95 = worst_S95;
    summary.num_endpoints = endpoint_count;
    summary.runtime_ms = duration;
    // Let's count weighted vs empty from bin_state
    size_t n_weighted = 0;
    size_t n_empty = 0;
    size_t n_inter = 0;
    if (config.mode >= UncertaintyMode::BIN_AWARE_VAR) {
        for(size_t i=0; i<fg.is_interconnect_edge.size(); ++i) {
            if (fg.is_interconnect_edge[i]) {
                n_inter++;
                if (!bin_state.edge_bin_weights[i].empty()) n_weighted++;
                else n_empty++;
            }
        }
    }
    summary.num_interconnect_edges = n_inter;
    summary.num_weighted_edges = n_weighted;
    summary.num_empty_weight_edges = n_empty;

    return summary;
}
