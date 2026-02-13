#pragma once

#include <vector>
#include <map>
#include <limits>
#include <cmath>
#include "tatum/TimingGraphFwd.hpp"

namespace tatum {
    class SetupTimingAnalyzer;
    class DelayCalculator;
}

/**
 * @brief A static view of the VPR Timing Graph, organized for factor-graph-style analysis.
 * 
 * Variable Mapping:
 * - A[v]: Arrival time at node v (mapping to tatum::NodeId)
 * - D[e]: Delay of edge e (mapping to tatum::EdgeId)
 * - B[e]: Candidate arrival time from edge e = A[src(e)] + D[e]
 * 
 * Factor Mapping:
 * - Sum Factor: A[src(e)] + D[e] = B[e]
 * - Max Factor: A[dst(v)] = max(B[e1], B[e2], ...) for all in_edges e_i of v
 */
struct GaussianMoments {
    double mu = 0.0;
    double var = 0.0;

    static GaussianMoments unset() {
        return {std::numeric_limits<double>::quiet_NaN(), 0.0};
    }

    bool is_set() const {
        return !std::isnan(mu);
    }
};

enum class UncertaintyMode {
    DETERMINISTIC = 0,    // alpha=0, gamma=0
    INDEPENDENT = 1,      // alpha>0, gamma=0
    BIN_AWARE_VAR = 2,    // alpha>0, gamma>0, rho=0
    BIN_LATENT_CORR = 3   // alpha>0, gamma>0, rho>0
};

struct ProbTimingConfig {
    UncertaintyMode mode = UncertaintyMode::DETERMINISTIC;
    float alpha = 0.0f;
    float gamma = 0.0f;
    int bins_x = 10;
    int bins_y = 10;
    bool forced_binning = false; // Validation harness (forces all edges to bin 0)
};

struct MomentStats {
    size_t num_max_calls_total = 0;
    size_t num_max_calls_sigma_both_zero = 0;
    size_t num_max_calls_sigma_one_zero = 0;
    size_t num_max_calls_general = 0;

    size_t num_min_calls_total = 0;
};

struct ProbTimingSummary {
    double worst_slack_95 = 0.0;
    int num_endpoints = 0;
    tatum::NodeId worst_endpoint_node_id = tatum::NodeId::INVALID();
    double runtime_ms = 0.0;
    size_t num_interconnect_edges = 0;
    size_t num_weighted_edges = 0;
    size_t num_empty_weight_edges = 0;
    MomentStats moments;
};

MomentStats get_moment_stats();
void reset_moment_stats();

struct FactorGraphView {
    // Core timing graph topology
    size_t num_nodes = 0;
    size_t num_edges = 0;
    
    // Topological order of all nodes
    std::vector<tatum::NodeId> topo_nodes;

    // Adjacency information
    std::vector<std::vector<tatum::EdgeId>> in_edges;  // indexed by NodeId
    std::vector<std::vector<tatum::EdgeId>> out_edges; // indexed by NodeId
    std::vector<tatum::NodeId> edge_src; // indexed by EdgeId
    std::vector<tatum::NodeId> edge_dst; // indexed by EdgeId

    // Factor-graph bookkeeping
    std::vector<int> candidate_id_for_edge; // indexed by EdgeId
    std::vector<std::vector<int>> candidate_ids_into_node; // indexed by NodeId

    // Classification
    std::vector<bool> is_interconnect_edge; 

    // Persistent Moment Buffers (Pre-allocated to avoid runtime allocation)
    std::vector<GaussianMoments> mu_var_A; // Arrival [node]
    std::vector<GaussianMoments> mu_var_R; // Required [node]
    std::vector<GaussianMoments> mu_var_B; // Candidate Arrival [edge]
    std::vector<GaussianMoments> mu_var_S; // Slack [endpoint_node]

    // Mode 3: Latent Sensitivities [node][bin_id]
    // Values are coefficients alpha_{v,b} such that A_v = mu_v + sum(alpha_{v,b} * Z_b) + independent_noise
    std::vector<std::vector<double>> mu_var_A_sens; 

    // Summary stats
    size_t reconvergent_node_count = 0;
    std::map<int, size_t> fanin_histogram;

    /**
     * @brief Computes a hash of the topology to prove invariance.
     */
    size_t get_structural_hash() const;
};

struct BinState {
    int bins_x = 0;
    int bins_y = 0;
    int num_bins = 0;
    
    // Bin Risk Parameters (tau^2 = gamma * C[bin])
    std::vector<double> bin_costs; // C[bin]

    // Edge-to-Bin Weights (Sparse)
    // For each edge e, we store a list of (bin_id, weight) pairs.
    // Normalized such that sum(weight) = 1.0 for each edge.
    // Only interconnect edges have entries here.
    std::vector<std::vector<std::pair<int, float>>> edge_bin_weights; 
};

/**
 * @brief Builds the FactorGraphView exactly once.
 */
FactorGraphView build_factor_graph_view(const tatum::TimingGraph& tg);

/**
 * @brief Performs Level C probabilistic timing analysis.
 * This is the single API for Modes 0-3.
 */
ProbTimingSummary run_probabilistic_timing(FactorGraphView& fg, 
                                 const tatum::TimingGraph& tg,
                                 const tatum::SetupTimingAnalyzer& analyzer,
                                 const tatum::DelayCalculator& delay_calc,
                                 const BinState& bin_state,
                                 const ProbTimingConfig& config);

void update_bin_state(const FactorGraphView& fg, BinState& bin_state, const ProbTimingConfig& config);
