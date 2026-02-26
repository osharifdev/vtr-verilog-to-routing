#include "raiga.h"
#include "vpr_context.h"
#include "route_common.h"
#include "route_export.h"
#include "route.h"
#include "vtr_time.h"
#include "vtr_log.h"
#include <cstdio>
#include <limits>
#include "concrete_timing_info.h"
#include "placer_state.h"

namespace raiga {

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
                             float time_limit_s) {
    VTR_LOG("  [RAIGA] Entered probe_route()\n"); fflush(stdout);
    vtr::Timer probe_timer;
    auto& route_ctx = g_vpr_ctx.mutable_routing();
    auto& device_ctx = g_vpr_ctx.device();

    VTR_LOG("  [RAIGA] Starting CP2 Probe Route (max_iters=%d, time_cap=%.1fs)...\n", max_iters, time_limit_s);
    fflush(stdout);

    // 1. Prepare limited router options
    t_router_opts probe_opts = router_opts;
    probe_opts.max_router_iterations = max_iters;
    probe_opts.router_time_limit_s = time_limit_s;
    probe_opts.route_verbosity = 0; // Quiet
    probe_opts.doRouting = e_stage_action::DO;
    
    // Disable timing analysis for probe in Step 1 to keep it fast and simple
    probe_opts.with_timing_analysis = false;

    // 2. Determine channel width factor
    int width_fac = router_opts.fixed_channel_width; 
    if (width_fac <= 0) width_fac = device_ctx.chan_width.max;
    if (width_fac <= 0) width_fac = 100; // Fallback if not initialized

    // 3. Execution using the standard VPR route() entry point.
    // This will handle RR graph creation (if needed) and struct allocation.
    NetPinsMatrix<float> net_delay = make_net_pins_matrix<float>(net_list); 
    
    VTR_LOG("  [RAIGA] Calling route()...\n"); fflush(stdout);
    bool status = route((const Netlist<>&)net_list,
                       width_fac,
                       probe_opts,
                       crr_opts,
                       analysis_opts,
                       const_cast<t_det_routing_arch&>(det_routing_arch),
                       const_cast<std::vector<t_segment_inf>&>(segment_inf),
                       net_delay,
                       make_constant_timing_info(0), // timing_info
                       nullptr, // delay_calc
                       chan_width_dist,
                       directs,
                       ScreenUpdatePriority::MINOR,
                       is_flat);
    VTR_LOG("  [RAIGA] route() returned status=%d\n", status); fflush(stdout);

    // 4. Extract Metrics
    ProbeRouteMetrics metrics;
    metrics.runtime_s = probe_timer.elapsed_sec();
    
    size_t total_ovf = 0;
    int max_ovf = 0;
    for (const RRNodeId& rr_id : device_ctx.rr_graph.nodes()) {
        int occ = route_ctx.rr_node_route_inf[rr_id].occ();
        int capacity = device_ctx.rr_graph.node_capacity(rr_id);
        if (occ > capacity) {
            int overuse = occ - capacity;
            total_ovf += overuse;
            max_ovf = std::max(max_ovf, overuse);
        }
    }
    metrics.total_overflow = (float)total_ovf;
    metrics.max_overflow = (float)max_ovf;

    VTR_LOG("  [RAIGA] Probe Complete: runtime=%.3fs total_ovf=%.0f max_ovf=%d success=%d\n", 
             metrics.runtime_s, metrics.total_overflow, max_ovf, (int)status);

    // 5. Full Reset (Mandatory)
    // Wipe routing state so g_vpr_ctx is clean for subsequent VPR stages.
    route_ctx.route_trees.clear();
    route_ctx.trace_nodes.clear();
    
    // Reset all occupancy and costs in the route info
    for (const RRNodeId& rr_id : device_ctx.rr_graph.nodes()) {
        auto& inf = route_ctx.rr_node_route_inf[rr_id];
        inf.set_occ(0);
        inf.path_cost = std::numeric_limits<float>::infinity();
        inf.backward_path_cost = std::numeric_limits<float>::infinity();
        inf.prev_edge = RREdgeId::INVALID();
        inf.acc_cost = 1.0f; 
    }

    return metrics;
}


CP1RudyMetrics compute_rudy_grid(const DeviceGrid& grid,
                                 const ClusteredNetlist& net_list,
                                 const PlacerState& placer_state,
                                 float user_theta) {
    CP1RudyMetrics metrics;
    int W = grid.width();
    int H = grid.height();

    // 1. Allocate 2D grid D[w][h] initialized to 0.0
    vtr::Matrix<float> D({size_t(W), size_t(H)}, 0.0f);

    int num_nets_included = 0;

    // 2. Iterate nets and populate grid
    for (auto net_id : net_list.nets()) {
        if (net_list.net_is_global(net_id) || net_list.net_is_ignored(net_id)) continue;
        
        auto pins = net_list.net_pins(net_id);
        if (pins.size() < 2) continue; // Ignore degenerate nets

        int xmin = std::numeric_limits<int>::max();
        int xmax = std::numeric_limits<int>::min();
        int ymin = std::numeric_limits<int>::max();
        int ymax = std::numeric_limits<int>::min();

        // Calculate bounding box based on placed cluster blocks
        for (auto pin_id : pins) {
            auto block_id = net_list.pin_block(pin_id);
            const auto& loc = placer_state.block_locs()[block_id].loc;
            int bx = loc.x;
            int by = loc.y;
            xmin = std::min(xmin, bx);
            xmax = std::max(xmax, bx);
            ymin = std::min(ymin, by);
            ymax = std::max(ymax, by);
        }

        // Clamp to grid
        xmin = std::max(0, std::min(xmin, W - 1));
        xmax = std::max(0, std::min(xmax, W - 1));
        ymin = std::max(0, std::min(ymin, H - 1));
        ymax = std::max(0, std::min(ymax, H - 1));

        int width = (xmax - xmin) + 1;
        int height = (ymax - ymin) + 1;
        float area = (float)(width * height);
        
        if (area <= 0.0f) continue;

        // Add uniform demand (1.0 / Area) to overlapped grid bins
        float demand_per_bin = 1.0f / area;
        for (int x = xmin; x <= xmax; ++x) {
            for (int y = ymin; y <= ymax; ++y) {
                D[x][y] += demand_per_bin;
            }
        }
        num_nets_included++;
    }

    metrics.num_nets_included = num_nets_included;

    // 3. Extract metrics deterministically
    std::vector<float> d_flat;
    d_flat.reserve(W * H);
    for (int x = 0; x < W; ++x) {
        for (int y = 0; y < H; ++y) {
            d_flat.push_back(D[x][y]);
        }
    }

    // Sort to easily extract percentiles
    std::sort(d_flat.begin(), d_flat.end());
    
    if (d_flat.empty()) return metrics; // Edge case

    metrics.peak = d_flat.back();
    metrics.p95 = d_flat[std::min((size_t)(d_flat.size() * 0.95), d_flat.size() - 1)];
    float p90 = d_flat[std::min((size_t)(d_flat.size() * 0.90), d_flat.size() - 1)];

    metrics.theta_used = (user_theta > 0.0f) ? user_theta : p90;

    // 4. Calculate hotspot cost
    for (float density : d_flat) {
        float diff = density - metrics.theta_used;
        if (diff > 0.0f) {
            metrics.hotspot_cost += (diff * diff);
        }
    }

    return metrics;
}

} // namespace raiga

namespace raiga {

bool IncrementalRudy::is_net_ignored(ClusterNetId net_id, const ClusteredNetlist& net_list) const {
    if (net_list.net_is_global(net_id) || net_list.net_is_ignored(net_id)) return true;
    if (net_list.net_pins(net_id).size() < 2) return true;
    return false;
}

t_bb IncrementalRudy::compute_net_bbox(ClusterNetId net_id, 
                                       const ClusteredNetlist& net_list, 
                                       const PlacerState& placer_state, 
                                       const t_pl_blocks_to_be_moved* proposed_blocks) const {
    auto pins = net_list.net_pins(net_id);
    int xmin = std::numeric_limits<int>::max();
    int xmax = std::numeric_limits<int>::min();
    int ymin = std::numeric_limits<int>::max();
    int ymax = std::numeric_limits<int>::min();

    for (auto pin_id : pins) {
        auto block_id = net_list.pin_block(pin_id);
        
        t_pl_loc loc;
        bool found_proposed = false;
        if (proposed_blocks != nullptr) {
            for (const auto& moved_blk : proposed_blocks->moved_blocks) {
                if (moved_blk.block_num == block_id) {
                    loc = moved_blk.new_loc;
                    found_proposed = true;
                    break;
                }
            }
        }
        if (!found_proposed) {
            loc = placer_state.block_locs()[block_id].loc;
        }

        int bx = loc.x;
        int by = loc.y;
        xmin = std::min(xmin, bx);
        xmax = std::max(xmax, bx);
        ymin = std::min(ymin, by);
        ymax = std::max(ymax, by);
    }
    
    t_bb bbox;
    bbox.xmin = xmin;
    bbox.xmax = xmax;
    bbox.ymin = ymin;
    bbox.ymax = ymax;
    return bbox;
}

void IncrementalRudy::compute_net_coverage(ClusterNetId net_id, 
                                           const t_bb& bbox,
                                           float area, 
                                           CachedNetInfo& info) const {
    info.bbox = bbox;
    info.covered_bins.clear();
    
    // Determine overlapped bin rectangle
    int bin_xmin = std::max(0, std::min(bins_x_ - 1, (int)(bbox.xmin / bin_w_)));
    int bin_xmax = std::max(0, std::min(bins_x_ - 1, (int)(bbox.xmax / bin_w_)));
    int bin_ymin = std::max(0, std::min(bins_y_ - 1, (int)(bbox.ymin / bin_h_)));
    int bin_ymax = std::max(0, std::min(bins_y_ - 1, (int)(bbox.ymax / bin_h_)));

    int num_bins = (bin_xmax - bin_xmin + 1) * (bin_ymax - bin_ymin + 1);
    num_bins = std::max(1, num_bins); // Safety per spec
    
    // Demand distribution rule (deterministic)
    info.demand_per_bin = 1.0f / num_bins;
    
    // Iteration order MUST be deterministic (y-major order).
    for (int by = bin_ymin; by <= bin_ymax; ++by) {
        for (int bx = bin_xmin; bx <= bin_xmax; ++bx) {
            info.covered_bins.push_back(by * bins_x_ + bx);
        }
    }
}

void IncrementalRudy::init(const DeviceGrid& grid,
                           const ClusteredNetlist& net_list,
                           const PlacerState& placer_state,
                           float theta,
                           int bins_x,
                           int bins_y) {
    theta_ = theta;
    grid_width_ = grid.width();
    grid_height_ = grid.height();
    bins_x_ = bins_x;
    bins_y_ = bins_y;
    bin_w_ = std::ceil((float)grid_width_ / bins_x_);
    bin_h_ = std::ceil((float)grid_height_ / bins_y_);

    D_.assign(bins_x_ * bins_y_, 0.0f);
    net_info_.clear();
    net_info_.resize(net_list.nets().size());

    current_hotspot_cost_ = 0.0f;
    pending_deltas_.clear();
    pending_net_updates_.clear();
    pending_hotspot_cost_ = 0.0f;

    for (auto net_id : net_list.nets()) {
        if (is_net_ignored(net_id, net_list)) continue;

        t_bb bbox = compute_net_bbox(net_id, net_list, placer_state, nullptr);
        float area = std::max(1, (bbox.xmax - bbox.xmin + 1) * (bbox.ymax - bbox.ymin + 1));
        
        CachedNetInfo& info = net_info_[(size_t)net_id];
        compute_net_coverage(net_id, bbox, area, info);

        for (int bin_idx : info.covered_bins) {
            D_[bin_idx] += info.demand_per_bin;
        }
    }

    for (size_t i = 0; i < D_.size(); ++i) {
        if (D_[i] > theta_) {
            current_hotspot_cost_ += (D_[i] - theta_) * (D_[i] - theta_);
        }
    }
}

float IncrementalRudy::propose_move(const t_pl_blocks_to_be_moved& blocks_affected,
                                    const ClusteredNetlist& net_list,
                                    const PlacerState& placer_state) {
    VTR_ASSERT_SAFE_MSG(!pending_proposal_, "Recursive RA-IGA proposal or missing commit/revert detected");
    pending_proposal_ = true;

    pending_deltas_.clear();
    pending_net_updates_.clear();
    
    std::vector<std::pair<int, float>> delta_map; 
    
    std::vector<ClusterNetId> incident_nets;
    for (const auto& moved_blk : blocks_affected.moved_blocks) {
        ClusterBlockId block_id = moved_blk.block_num;
        for (auto pin_id : net_list.block_pins(block_id)) {
            ClusterNetId net_id = net_list.pin_net(pin_id);
            if (net_id && !is_net_ignored(net_id, net_list)) {
                incident_nets.push_back(net_id);
            }
        }
    }
    
    std::sort(incident_nets.begin(), incident_nets.end());
    incident_nets.erase(std::unique(incident_nets.begin(), incident_nets.end()), incident_nets.end());

    for (auto net_id : incident_nets) {
        const CachedNetInfo& old_info = net_info_[(size_t)net_id];
        
        for (int bin_idx : old_info.covered_bins) {
            delta_map.push_back({bin_idx, -old_info.demand_per_bin});
        }
        
        t_bb new_bbox = compute_net_bbox(net_id, net_list, placer_state, &blocks_affected);
        float new_area = std::max(1, (new_bbox.xmax - new_bbox.xmin + 1) * (new_bbox.ymax - new_bbox.ymin + 1));
        
        CachedNetInfo new_info;
        compute_net_coverage(net_id, new_bbox, new_area, new_info);
        
        for (int bin_idx : new_info.covered_bins) {
            delta_map.push_back({bin_idx, new_info.demand_per_bin});
        }
        
        pending_net_updates_.push_back({net_id, new_info});
    }

    std::sort(delta_map.begin(), delta_map.end());
    
    pending_hotspot_cost_ = current_hotspot_cost_;

    if (!delta_map.empty()) {
        int current_bin = delta_map[0].first;
        float current_delta = 0.0f;
        
        for (const auto& pair : delta_map) {
            if (pair.first == current_bin) {
                current_delta += pair.second;
            } else {
                if (std::abs(current_delta) > 1e-6) {
                    pending_deltas_.push_back({current_bin, current_delta});
                }
                current_bin = pair.first;
                current_delta = pair.second;
            }
        }
        if (std::abs(current_delta) > 1e-6) {
            pending_deltas_.push_back({current_bin, current_delta});
        }
    }

    for (const auto& delta_item : pending_deltas_) {
        int b = delta_item.bin_idx;
        float old_val = D_[b];
        float new_val = D_[b] + delta_item.delta;
        
        float old_cost = (old_val > theta_) ? (old_val - theta_) * (old_val - theta_) : 0.0f;
        float new_cost = (new_val > theta_) ? (new_val - theta_) * (new_val - theta_) : 0.0f;
        
        pending_hotspot_cost_ += (new_cost - old_cost);
    }
    
    return pending_hotspot_cost_ - current_hotspot_cost_;
}

void IncrementalRudy::commit_move() {
    for (const auto& delta_item : pending_deltas_) {
        D_[delta_item.bin_idx] += delta_item.delta;
    }
    for (const auto& update : pending_net_updates_) {
        net_info_[(size_t)update.first] = update.second;
    }
    current_hotspot_cost_ = pending_hotspot_cost_;
    
    pending_deltas_.clear();
    pending_net_updates_.clear();
    pending_proposal_ = false;
}

void IncrementalRudy::revert_move() {
    pending_deltas_.clear();
    pending_net_updates_.clear();
    pending_proposal_ = false;
}

bool IncrementalRudy::verify_equivalence(const DeviceGrid& grid,
                                         const ClusteredNetlist& net_list,
                                         const PlacerState& placer_state) const {
    std::vector<float> check_D(bins_x_ * bins_y_, 0.0f);
    float check_hotspot_cost = 0.0f;

    for (auto net_id : net_list.nets()) {
        if (is_net_ignored(net_id, net_list)) continue;

        t_bb bbox = compute_net_bbox(net_id, net_list, placer_state, nullptr);
        float area = std::max(1, (bbox.xmax - bbox.xmin + 1) * (bbox.ymax - bbox.ymin + 1));
        
        CachedNetInfo info;
        compute_net_coverage(net_id, bbox, area, info);

        for (int bin_idx : info.covered_bins) {
            check_D[bin_idx] += info.demand_per_bin;
        }
    }

    for (size_t i = 0; i < check_D.size(); ++i) {
        if (check_D[i] > theta_) {
            check_hotspot_cost += (check_D[i] - theta_) * (check_D[i] - theta_);
        }
    }

    bool pass = true;
    for (size_t i = 0; i < D_.size(); ++i) {
        if (std::abs(D_[i] - check_D[i]) > 1e-3) { // Use float tolerance
            VTR_LOG("RAIGA_DEBUG_EQUIVALENCE FAIL: Bin %zu mismatch. Incr: %f, Scratch: %f\n", i, D_[i], check_D[i]);
            pass = false;
        }
    }
    
    if (std::abs(current_hotspot_cost_ - check_hotspot_cost) > 1e-3) {
        VTR_LOG("RAIGA_DEBUG_EQUIVALENCE FAIL: Hotspot cost mismatch. Incr: %f, Scratch: %f\n", current_hotspot_cost_, check_hotspot_cost);
        pass = false;
    }

    if (pass) {
        VTR_LOG("RAIGA_DEBUG_EQUIVALENCE: PASS\n");
    }
    return pass;
}

float IncrementalRudy::get_peak_demand() const {
    float peak = 0.0f;
    for (float v : D_) {
        if (v > peak) peak = v;
    }
    return peak;
}

} // namespace raiga
