#pragma once

#include <vector>
#include <map>
#include <limits>
#include <cmath>
#include "tatum/TimingGraphFwd.hpp"
#include "vpr_types.h"
#include "vtr_vector_map.h"

namespace tatum {
    class SetupTimingAnalyzer;
    class DelayCalculator;
}

/**
 * @brief A defensive wrapper for std::vector that ensures bounds-checked access.
 */
template <typename T>
struct SafeVector : public std::vector<T> {
    using std::vector<T>::vector;
    T& operator[](size_t n) {
        if (n >= this->size()) {
            static T dummy{};
            return dummy;
        }
        return std::vector<T>::operator[](n);
    }
    const T& operator[](size_t n) const {
        if (n >= this->size()) {
            static const T dummy{};
            return dummy;
        }
        return std::vector<T>::operator[](n);
    }
};

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
    DETERMINISTIC = 0,
    INDEPENDENT = 1,
    BIN_LATENT_CORR = 3,
    PHYSICAL_COMBINED = 4
};

struct ProbTimingConfig {
    UncertaintyMode mode = UncertaintyMode::DETERMINISTIC;
    float alpha = 0.0f;
    float beta = 0.0f;
    float gamma = 0.0f;          // [PHASE 7] Congestion sensitivity
    float huber_delta = 20.0;
    bool forced_binning = false; // Validation harness

    // [PHASE 7.1] Stability v2
    float slack_gate = 0.0f;           // Proactive mask threshold
    float hallucination_dampen = 1.0f; // Reactive damper
    float momentum_boost = 1.0f;       // Success boost
    float min_slack = 0.0f;            // Worsted slack in the circuit
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
    std::vector<int> node_levels;  // [NEW] Node levels for logic-depth scaling

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
    SafeVector<GaussianMoments> mu_var_A; // Arrival [node]
    SafeVector<GaussianMoments> mu_var_R; // Required [node]
    SafeVector<GaussianMoments> mu_var_B; // Candidate Arrival [edge]
    SafeVector<GaussianMoments> mu_var_S; // Slack [endpoint_node]



    // Summary stats
    size_t reconvergent_node_count = 0;
    float reconvergence_density = 0.0f; // [NEW] Ratio of fanin > 1 nodes
    float median_logic_depth = 0.0f;     // [NEW] Median topological level
    std::map<int, size_t> fanin_histogram;

    /**
     * @brief Computes a hash of the topology to prove invariance.
     */
    size_t get_structural_hash() const;
};

struct PhysicalState {
    // Edge-Specific Physical Scaling Factors
    // S_e = 1.0 + beta * distance(u,v)
    // Indexed by EdgeId (sparse, mostly for interconnect)
    SafeVector<float> edge_phys_scales; 
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
                                 const PhysicalState& phys_state,
                                 const ProbTimingConfig& config);

class NetCostHandler;

void update_physical_state(const FactorGraphView& fg, 
                      PhysicalState& phys_state, 
                      const ProbTimingConfig& config,
                      const vtr::vector_map<ClusterBlockId, t_block_loc>& block_locs,
                      const NetCostHandler* net_cost_handler = nullptr);
