#pragma once

#include "vpr_types.h"
#include "clustered_netlist.h"
#include "placer_state.h"
#include "move_transactions.h"

namespace raiga {

struct ProbeRouteMetrics {
    float total_overflow = 0.0f;
    float max_overflow = 0.0f;
    float overflow_slope = 0.0f; // Future use
    float runtime_s = 0.0f;
};

struct CP1RudyMetrics {
    float peak = 0.0f;
    float p95 = 0.0f;
    float hotspot_cost = 0.0f;
    float theta_used = 0.0f;
    int num_nets_included = 0;
};

/**
 * @brief Computes RUDY-based congestion grid metrics at Step 1.
 */
CP1RudyMetrics compute_rudy_grid(const DeviceGrid& grid,
                                 const ClusteredNetlist& net_list,
                                 const PlacerState& placer_state,
                                 float user_theta);

/**
 * @brief Performs a non-destructive probe route to assess current placement routability.
 * 
 * This function preserves no state. It initializes routing structures from scratch,
 * runs the router with strict caps (max iters, time limit), extracts congestion
 * metrics, and then fully resets the routing structures.
 */
ProbeRouteMetrics probe_route(const ClusteredNetlist& net_list,
                             const t_router_opts& router_opts,
                             const t_crr_opts& crr_opts,
                             const t_analysis_opts& analysis_opts,
                             const t_chan_width_dist& chan_width_dist,
                             const t_det_routing_arch& det_routing_arch,
                             const std::vector<t_segment_inf>& segment_inf,
                             const std::vector<t_direct_inf>& directs,
                             bool is_flat,
                             int max_iters,
                             float time_limit_s);

struct BinDelta {
    int bin_idx;
    float delta;
};

class IncrementalRudy {
public:
    void init(const DeviceGrid& grid,
              const ClusteredNetlist& net_list,
              const PlacerState& placer_state,
              float theta,
              int bins_x,
              int bins_y);

    float propose_move(const t_pl_blocks_to_be_moved& blocks_affected,
                       const ClusteredNetlist& net_list,
                       const PlacerState& placer_state);

    void commit_move();
    void revert_move();

    bool verify_equivalence(const DeviceGrid& grid,
                            const ClusteredNetlist& net_list,
                            const PlacerState& placer_state) const;

    float get_hotspot_cost() const { return current_hotspot_cost_; }
    float get_peak_demand() const;

private:
    float theta_ = 0.0f;
    int grid_width_ = 0;
    int grid_height_ = 0;
    int bins_x_ = 0;
    int bins_y_ = 0;
    int bin_w_ = 0;
    int bin_h_ = 0;

    std::vector<float> D_; // Flattened 2D grid, size bins_x_ * bins_y_
    
    struct CachedNetInfo {
        t_bb bbox;
        std::vector<int> covered_bins;
        float demand_per_bin = 0.0f;
    };
    std::vector<CachedNetInfo> net_info_; // Indexed by ClusterNetId

    float current_hotspot_cost_ = 0.0f;

    // Temporary/Tentative state for proposals
    std::vector<BinDelta> pending_deltas_;
    std::vector<std::pair<ClusterNetId, CachedNetInfo>> pending_net_updates_;
    float pending_hotspot_cost_ = 0.0f;
    bool pending_proposal_ = false;

    // Helper functions
    void compute_net_coverage(ClusterNetId net_id, 
                              const t_bb& bbox,
                              float area, 
                              CachedNetInfo& info) const;
    
    t_bb compute_net_bbox(ClusterNetId net_id, 
                          const ClusteredNetlist& net_list, 
                          const PlacerState& placer_state, 
                          const t_pl_blocks_to_be_moved* proposed_blocks = nullptr) const;

    bool is_net_ignored(ClusterNetId net_id, const ClusteredNetlist& net_list) const;
};

} // namespace raiga
