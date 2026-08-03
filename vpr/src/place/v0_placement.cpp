#include "v0_placement.h"
#include "v0_configs.h"
#include "globals.h"
#include "vpr_error.h"
#include "initial_placement.h"
#include "place_constraints.h"
#include "vtr_random.h"
#include "vtr_log.h"
#include "vtr_time.h"
#include "FactorGraphView.h"
#include "timing_util.h"
#include "place_util.h"
#include "PlacerCriticalities.h"
#include "vpr_utils.h"
#include "clustered_netlist_utils.h"
#include <cmath>
#include <algorithm>
#include <vector>
#include <set>
#include <numeric>
#include "tatum/delay_calc/FixedDelayCalculator.hpp"
#include "tatum/analyzer_factory.hpp"

// --- Final Constraints ---
// Option A: Purely Logical Delay Model
static constexpr float D_INTER = 1.0e-9; // 1.0 ns for numerical stability

/**
 * @brief Logic for V0 Initial Placement
 */
class V0Engine {
public:
    V0Engine(const t_placer_opts& opts, BlkLocRegistry& registry, const PlaceMacros& macros)
        : opts_(opts), registry_(registry), macros_(macros) {}

    void run() {
        vtr::ScopedStartFinishTimer timer("V0 Deterministic Initial Placement");

        // 1. Handle Hard Macros if present
        if (!macros_.macros().empty()) {
            if (opts_.v0_macro_mode == 0) {
                // Method 0: Direct Macro Placement (Baseline-style random initialization)
                VTR_LOG("[V0_FLOW] Executing Mode 0: Direct Macro Placement\n");
                vtr::RngContainer rng(opts_.seed);
                const auto& floorplanning_ctx = g_vpr_ctx.floorplanning();
                auto& cluster_ctx = g_vpr_ctx.clustering();
                for (const t_pl_macro& macro : macros_.macros()) {
                    ClusterBlockId blk_id = macro.members[0].blk_index;
                    auto block_type = cluster_ctx.clb_nlist.block_type(blk_id);
                    const PartitionRegion& pr = (is_cluster_constrained(blk_id)) 
                        ? floorplanning_ctx.cluster_constraints[blk_id] 
                        : get_device_partition_region();

                    bool placed = false;
                    for (int itry = 0; itry < MAX_NUM_TRIES_TO_PLACE_MACROS_RANDOMLY && !placed; itry++) {
                        placed = try_place_macro_randomly(macro, pr, block_type, opts_.pad_loc_type, registry_, rng);
                    }
                    if (!placed) {
                        placed = try_place_macro_exhaustively(macro, pr, block_type, opts_.pad_loc_type, registry_);
                    }
                    if (!placed) {
                        VPR_FATAL_ERROR(VPR_ERROR_PLACE, "V0: Could not place macro!\n");
                    }
                    // Lock macro into V0 fixed blocks
                    for (const auto& member : macro.members) {
                        fixed_blocks_.insert(member.blk_index);
                        registry_.mutable_block_locs()[member.blk_index].is_fixed = true;
                    }
                }
            } else if (opts_.v0_macro_mode == 1) {
                VTR_LOG("[V0_FLOW] Executing Mode 1: Dynamic Macro Placement\n");
                
                // Allow V0 inference and a few linear iterations to run first 
                // to let the macros float to their globally optimal centers.
                // We will implement this below by running the first few V0 iterations WITHOUT fixing macros,
                // then locking them statically.
            }
        }

        auto& cluster_ctx = g_vpr_ctx.clustering();

        // 2. Identify Fixed Blocks (Anchors)
        std::vector<ClusterBlockId> movable_blocks;
        for (auto blk_id : cluster_ctx.clb_nlist.blocks()) {
            if (registry_.block_locs()[blk_id].is_fixed) {
                fixed_blocks_.insert(blk_id);
            } else {
                movable_blocks.push_back(blk_id);
            }
        }

        // 3. Precompute Stable Netlist Layouts
        precompute_stable_ordering();

        // 4. Perform Logical-STA-Pass (Inference)
        run_inference();

        // 4. Compute construction order (Sorted by Bucketed Crit95)
        std::vector<ClusterBlockId> sorted_blocks = movable_blocks;
        bool is_stable = (opts_.v0_ordering_mode == "stable");

        // Config-specific salt: deterministic function of prob_config_id.
        // Phi-based multiplicative salt ensures configs 0..23 produce distinct orderings
        // when name hashes are XOR-mixed. Only active in stable mode.
        const uint32_t cfg_salt = is_stable
            ? (static_cast<uint32_t>(0x9e3779b9u) * static_cast<uint32_t>(opts_.prob_config_id + 1))
            : 0u;

        // Per-block salt-mixed key: used to differentiate equal-crit-bucket blocks
        // across configs without adding randomness.
        auto block_sort_key = [&](ClusterBlockId id) -> uint32_t {
            const std::string& nm = cluster_ctx.clb_nlist.block_name(id);
            // FNV-1a hash of the block name, then XOR with config salt
            uint32_t h = 2166136261u;
            for (unsigned char c : nm) { h ^= c; h *= 16777619u; }
            return h ^ cfg_salt;
        };

        std::sort(sorted_blocks.begin(), sorted_blocks.end(),
                  [this, &cluster_ctx, is_stable, &block_sort_key](ClusterBlockId a, ClusterBlockId b) {
            float crit_a = get_block_crit(a);
            float crit_b = get_block_crit(b);

            int bucket_a = std::round(crit_a * 1e6f);
            int bucket_b = std::round(crit_b * 1e6f);

            if (bucket_a != bucket_b) {
                return bucket_a > bucket_b; // Descending criticality
            }
            if (is_stable) {
                // Salted tie-break: config-specific hash of name XOR salt
                uint32_t key_a = block_sort_key(a);
                uint32_t key_b = block_sort_key(b);
                if (key_a != key_b) return key_a < key_b;
                // Further tie-break: name then id (deterministic)
                const std::string& name_a = cluster_ctx.clb_nlist.block_name(a);
                const std::string& name_b = cluster_ctx.clb_nlist.block_name(b);
                if (name_a != name_b) return name_a < name_b;
            }
            return a < b;
        });


        // Debug dump: first 10 blocks in placement order
        if (opts_.v0_debug) {
            VTR_LOG("[V0_DEBUG] seed=%d mode=%s: First 10 blocks in placement order:\n", opts_.seed, opts_.v0_ordering_mode.c_str());
            for (int i = 0; i < (int)sorted_blocks.size() && i < 10; ++i) {
                ClusterBlockId bid = sorted_blocks[i];
                int bucket = std::round(get_block_crit(bid) * 1e6f);
                VTR_LOG("  [V0_DEBUG_BLOCK] i=%d blk_id=%d name=%s crit_bucket=%d\n",
                        i, size_t(bid), cluster_ctx.clb_nlist.block_name(bid).c_str(), bucket);
            }

            // Debug dump: first 10 nets in inference order and first 5 sinks each
            auto debug_net_range = (is_stable)
                ? vtr::make_range(stable_nets_.cbegin(), stable_nets_.cend())
                : cluster_ctx.clb_nlist.nets();
            int net_count = 0;
            for (auto net_id : debug_net_range) {
                if (net_count >= 10) break;
                VTR_LOG("  [V0_DEBUG_NET] i=%d net_id=%d name=%s omega=%.6f\n",
                        net_count, size_t(net_id), cluster_ctx.clb_nlist.net_name(net_id).c_str(),
                        net_omega_[size_t(net_id)]);
                // first 5 sinks
                auto debug_sink_range = (is_stable)
                    ? vtr::make_range(stable_net_sinks_[size_t(net_id)].cbegin(), stable_net_sinks_[size_t(net_id)].cend())
                    : cluster_ctx.clb_nlist.net_sinks(net_id);
                int sink_count = 0;
                for (auto pin_id : debug_sink_range) {
                    if (sink_count >= 5) break;
                    ClusterBlockId blk = cluster_ctx.clb_nlist.pin_block(pin_id);
                    VTR_LOG("    [V0_DEBUG_SINK] sink=%d pin_id=%d blk_id=%d blk_name=%s\n",
                            sink_count, size_t(pin_id), size_t(blk),
                            cluster_ctx.clb_nlist.block_name(blk).c_str());
                    ++sink_count;
                }
                ++net_count;
            }
        }

        // 5. Sequential Barycenter Placement
        int placed_count = 0;
        int zero_weight_fallback_count = 0;
        int empty_nets_count = 0;

        std::set<ClusterBlockId> macro_members;
        for (const t_pl_macro& macro : macros_.macros()) {
            for (const auto& member : macro.members) {
                macro_members.insert(member.blk_index);
            }
        }

        for (auto blk_id : sorted_blocks) {
            if (placed_blocks_.count(blk_id)) continue;

            t_pl_loc target = compute_barycenter(blk_id, empty_nets_count, zero_weight_fallback_count);
            
            if (macro_members.count(blk_id)) {
                // Find the macro it belongs to
                t_pl_macro macro;
                for (const auto& m : macros_.macros()) {
                    bool found = false;
                    for (const auto& mem : m.members) {
                        if (mem.blk_index == blk_id) { found = true; break; }
                    }
                    if (found) { macro = m; break; }
                }

                if (opts_.v0_macro_mode == 1) {
                    // Perturb target based on config_id to get diverse macro basins
                    int dx = (opts_.prob_config_id * 17) % 7 - 3;
                    int dy = (opts_.prob_config_id * 23) % 7 - 3;
                    target.x = std::max(0, std::min((int)g_vpr_ctx.device().grid.width() - 1, target.x + dx));
                    target.y = std::max(0, std::min((int)g_vpr_ctx.device().grid.height() - 1, target.y + dy));
                }

                t_pl_loc legal_loc = find_legal_macro_spiral(macro, target);
                
                // try_place_macro internally updates registry, just mark as placed here
                for (const auto& mem : macro.members) {
                    placed_blocks_.insert(mem.blk_index);
                    placed_count++;
                }
            } else {
                // Legalize via standard 1x1 spiral search
                t_pl_loc legal_loc = find_legal_spiral(blk_id, target);
                registry_.set_block_location(blk_id, legal_loc);
                placed_blocks_.insert(blk_id);
                placed_count++;
            }
        }

        VTR_LOG("V0_SUMMARY: Placed %d blocks. Fixed: %zu. ZeroWeightFallbacks: %d. EmptyNets: %d.\n",
                placed_count, fixed_blocks_.size(), zero_weight_fallback_count, empty_nets_count);
        
        log_sensitivity_metrics();

        // 6. Print V0_HASH
        std::vector<ClusterBlockId> hash_blocks;
        for (auto blk_id : cluster_ctx.clb_nlist.blocks()) {
            hash_blocks.push_back(blk_id);
        }
        if (opts_.v0_ordering_mode == "stable") {
            std::sort(hash_blocks.begin(), hash_blocks.end(), [&cluster_ctx](ClusterBlockId a, ClusterBlockId b) {
                const std::string& name_a = cluster_ctx.clb_nlist.block_name(a);
                const std::string& name_b = cluster_ctx.clb_nlist.block_name(b);
                if (name_a != name_b) return name_a < name_b;
                return a < b;
            });
        } // Native mode hashes exactly by cluster_ctx.clb_nlist.blocks() default iteration!
        
        unsigned long long place_hash = 0;
        for (auto blk_id : hash_blocks) {
            t_block_loc loc = registry_.block_locs()[blk_id];
            place_hash = (place_hash * 31) + loc.loc.x;
            place_hash = (place_hash * 31) + loc.loc.y;
            place_hash = (place_hash * 31) + loc.loc.sub_tile;
            place_hash = (place_hash * 31) + size_t(blk_id);
        }
        
        // Use the atom netlist name as a reliable circuit identifier.
        // It is set from the BLIF file during atom netlist construction.
        const std::string& circuit_name = g_vpr_ctx.atom().netlist().netlist_name();
        VTR_LOG("\n[V0_HASH] circuit=%s, seed=%d, pcfg=%d, mode=%s, hash=%llu\n\n",
                circuit_name.c_str(), opts_.seed, opts_.prob_config_id,
                opts_.v0_ordering_mode.c_str(), place_hash);

        // Part D: Deterministic HPWL recompute
        // Pure function of block coordinates: nets in net_id ascending order,
        // all pins iterated pin_id ascending, accumulation in double.
        // Must be identical for identical V0_HASH.
        {
            // Collect all net IDs sorted ascending by net_id integer value
            std::vector<ClusterNetId> sorted_nets;
            sorted_nets.reserve(cluster_ctx.clb_nlist.nets().size());
            for (auto net_id : cluster_ctx.clb_nlist.nets()) {
                sorted_nets.push_back(net_id);
            }
            std::sort(sorted_nets.begin(), sorted_nets.end());

            double bb_recomp = 0.0;
            for (auto net_id : sorted_nets) {
                // Collect all pins of this net sorted ascending by pin_id
                std::vector<ClusterPinId> sorted_pins;
                for (auto pin_id : cluster_ctx.clb_nlist.net_pins(net_id)) {
                    sorted_pins.push_back(pin_id);
                }
                std::sort(sorted_pins.begin(), sorted_pins.end());

                int xmin = std::numeric_limits<int>::max();
                int xmax = std::numeric_limits<int>::min();
                int ymin = std::numeric_limits<int>::max();
                int ymax = std::numeric_limits<int>::min();

                for (auto pin_id : sorted_pins) {
                    ClusterBlockId blk = cluster_ctx.clb_nlist.pin_block(pin_id);
                    t_block_loc loc = registry_.block_locs()[blk];
                    xmin = std::min(xmin, loc.loc.x);
                    xmax = std::max(xmax, loc.loc.x);
                    ymin = std::min(ymin, loc.loc.y);
                    ymax = std::max(ymax, loc.loc.y);
                }
                if (xmin <= xmax) {
                    // HPWL contribution: sum (xmax-xmin) + (ymax-ymin)
                    bb_recomp += (double)(xmax - xmin) + (double)(ymax - ymin);
                }
            }
            // TIMING_COST_RECOMP: not available in V0 scope (timing analyzer lives in Placer)
            VTR_LOG("[V0_BB_RECOMP] circuit=%s, seed=%d, pcfg=%d, mode=%s, bb_hpwl=%.15g\n",
                    circuit_name.c_str(), opts_.seed, opts_.prob_config_id,
                    opts_.v0_ordering_mode.c_str(), bb_recomp);
            VTR_LOG("[V0_BASIN_SUMMARY] pcfg=%d, hash=%llu, hpwl=%.15g, salt=0x%08x\n",
                    opts_.prob_config_id, place_hash, bb_recomp,
                    static_cast<unsigned>(static_cast<uint32_t>(0x9e3779b9u)
                                         * static_cast<uint32_t>(opts_.prob_config_id + 1)));
        }
    }

private:
    const t_placer_opts& opts_;
    BlkLocRegistry& registry_;
    const PlaceMacros& macros_;

    std::set<ClusterBlockId> fixed_blocks_;
    std::set<ClusterBlockId> placed_blocks_;
    
    // Inferred data
    std::vector<float> net_omega_; // Net weights
    std::vector<float> block_crit_; // Block sorting key

    std::vector<ClusterNetId> stable_nets_;
    std::vector<std::vector<ClusterPinId>> stable_net_sinks_;
    std::vector<std::vector<ClusterPinId>> stable_block_pins_;
    std::vector<std::vector<ClusterPinId>> stable_net_pins_;

    void precompute_stable_ordering() {
        if (opts_.v0_ordering_mode != "stable") return;
        auto& cluster_ctx = g_vpr_ctx.clustering();

        // 1. Stable Nets
        for (auto net_id : cluster_ctx.clb_nlist.nets()) {
            stable_nets_.push_back(net_id);
        }
        std::sort(stable_nets_.begin(), stable_nets_.end(), [&cluster_ctx](ClusterNetId a, ClusterNetId b) {
            const std::string& name_a = cluster_ctx.clb_nlist.net_name(a);
            const std::string& name_b = cluster_ctx.clb_nlist.net_name(b);
            if (name_a != name_b) return name_a < name_b;
            return a < b;
        });

        int net_collisions = 0;
        if (opts_.v0_debug) {
            for (size_t i = 1; i < stable_nets_.size(); ++i) {
                if (cluster_ctx.clb_nlist.net_name(stable_nets_[i]) == cluster_ctx.clb_nlist.net_name(stable_nets_[i-1])) {
                    net_collisions++;
                }
            }
            if (net_collisions > 0) {
                VTR_LOG("V0_DEBUG: Found %d net name collisions during stable sort.\n", net_collisions);
            }
        }

        // 2. Stable Net Sinks and Net Pins
        stable_net_sinks_.resize(cluster_ctx.clb_nlist.nets().size());
        stable_net_pins_.resize(cluster_ctx.clb_nlist.nets().size());
        int block_name_collisions = 0;
        for (auto net_id : stable_nets_) {
            // Sinks
            auto sinks = cluster_ctx.clb_nlist.net_sinks(net_id);
            std::vector<ClusterPinId> sorted_sinks(sinks.begin(), sinks.end());
            
            std::sort(sorted_sinks.begin(), sorted_sinks.end(), [&cluster_ctx](ClusterPinId a, ClusterPinId b) {
                ClusterBlockId blk_a = cluster_ctx.clb_nlist.pin_block(a);
                ClusterBlockId blk_b = cluster_ctx.clb_nlist.pin_block(b);
                const std::string& name_a = cluster_ctx.clb_nlist.block_name(blk_a);
                const std::string& name_b = cluster_ctx.clb_nlist.block_name(blk_b);
                if (name_a != name_b) return name_a < name_b;
                if (blk_a != blk_b) return blk_a < blk_b;
                return a < b;
            });

            if (opts_.v0_debug) {
                for (size_t i = 1; i < sorted_sinks.size(); ++i) {
                    ClusterBlockId blk_a = cluster_ctx.clb_nlist.pin_block(sorted_sinks[i]);
                    ClusterBlockId blk_b = cluster_ctx.clb_nlist.pin_block(sorted_sinks[i-1]);
                    if (blk_a != blk_b && cluster_ctx.clb_nlist.block_name(blk_a) == cluster_ctx.clb_nlist.block_name(blk_b)) {
                        block_name_collisions++;
                    }
                }
            }
            stable_net_sinks_[size_t(net_id)] = sorted_sinks;

            // All Pins (Sinks + Drivers)
            auto npins = cluster_ctx.clb_nlist.net_pins(net_id);
            std::vector<ClusterPinId> sorted_npins(npins.begin(), npins.end());
            std::sort(sorted_npins.begin(), sorted_npins.end(), [&cluster_ctx](ClusterPinId a, ClusterPinId b) {
                ClusterBlockId blk_a = cluster_ctx.clb_nlist.pin_block(a);
                ClusterBlockId blk_b = cluster_ctx.clb_nlist.pin_block(b);
                const std::string& name_a = cluster_ctx.clb_nlist.block_name(blk_a);
                const std::string& name_b = cluster_ctx.clb_nlist.block_name(blk_b);
                if (name_a != name_b) return name_a < name_b;
                if (blk_a != blk_b) return blk_a < blk_b;
                return a < b;
            });
            stable_net_pins_[size_t(net_id)] = sorted_npins;
        }

        if (opts_.v0_debug && block_name_collisions > 0) {
            VTR_LOG("V0_DEBUG: Found %d block name collisions in net_sinks stable sort.\n", block_name_collisions);
        }

        // 3. Stable Block Pins (for barycenter stability)
        stable_block_pins_.resize(cluster_ctx.clb_nlist.blocks().size());
        for (auto blk_id : cluster_ctx.clb_nlist.blocks()) {
            auto pins = cluster_ctx.clb_nlist.block_pins(blk_id);
            std::vector<ClusterPinId> sorted_pins(pins.begin(), pins.end());

             std::sort(sorted_pins.begin(), sorted_pins.end(), [&cluster_ctx](ClusterPinId a, ClusterPinId b) {
                auto net_a = cluster_ctx.clb_nlist.pin_net(a);
                auto net_b = cluster_ctx.clb_nlist.pin_net(b);
                const std::string& name_a = cluster_ctx.clb_nlist.net_name(net_a);
                const std::string& name_b = cluster_ctx.clb_nlist.net_name(net_b);
                if (name_a != name_b) return name_a < name_b;
                if (net_a != net_b) return net_a < net_b;
                return a < b;
            });
            stable_block_pins_[size_t(blk_id)] = sorted_pins;
        }
    }

    void run_inference() {
        auto& cluster_ctx = g_vpr_ctx.clustering();
        auto& timing_ctx = g_vpr_ctx.timing();
        
        // Confirm timing constraints exist (Required times)
        if (!timing_ctx.constraints) {
            VPR_FATAL_ERROR(VPR_ERROR_OTHER, "V0: Timing constraints (SDC) not found. Required times are needed for Crit95. STOP.\n");
        }

        /* 1. Setup Probabilistic Engine */
        FactorGraphView fg = build_factor_graph_view(*timing_ctx.graph);
        
        // Logical STA setup (Option A: D_INTER = 1.0ns everywhere)
        tatum::util::linear_map<tatum::EdgeId, tatum::Time> edge_delays(timing_ctx.graph->edges().size(), tatum::Time(D_INTER));
        tatum::util::linear_map<tatum::EdgeId, tatum::Time> setup_times(timing_ctx.graph->edges().size(), tatum::Time(0.0));

        auto delay_calc = std::make_shared<tatum::FixedDelayCalculator>(edge_delays, setup_times);
        auto analyzer = tatum::AnalyzerFactory<tatum::SetupAnalysis>::make(*timing_ctx.graph, *timing_ctx.constraints, *delay_calc);
        analyzer->update_setup_timing();

        PhysicalState logical_state;
        logical_state.edge_phys_scales.resize(fg.num_nodes, 1.0f);

        ProbConfig pcfg;
        if (opts_.prob_config_id >= 0 && opts_.prob_config_id < 24) {
            pcfg = kProbConfigs[opts_.prob_config_id];
        } else {
            pcfg = {0, 0.15f, 1.0f, 0.10f, 0.0f, 1.1f}; // Default if not specified
        }

        ProbTimingConfig config;
        config.alpha = pcfg.alpha;
        config.beta = pcfg.beta;
        config.slack_gate = pcfg.gate;
        config.momentum_boost = pcfg.boost;
        config.mode = UncertaintyMode::PHYSICAL_COMBINED;
        config.min_slack = find_setup_worst_negative_slack(*analyzer);

        ProbTimingSummary summary = run_probabilistic_timing(fg, *timing_ctx.graph, *analyzer, *delay_calc, logical_state, config);

        // Compute Crit95 for nets and blocks
        net_omega_.assign(cluster_ctx.clb_nlist.nets().size(), 0.0f);
        block_crit_.assign(cluster_ctx.clb_nlist.blocks().size(), 0.0f);

        // Normalize Slack Risk (Risk Norm)
        // Heuristic: risk = sigma / mu (coefficient of variation) or simply variance scaling
        // Audit uses global risk_norm. Here we use a per-pin risk.
        float global_lambda = pcfg.lambda;

        const auto& atom_ctx = g_vpr_ctx.atom();
        const auto& device_ctx = g_vpr_ctx.device();

        IntraLbPbPinLookup pb_gpin_lookup(device_ctx.logical_block_types);
        ClusteredPinAtomPinsLookup pin_lookup(cluster_ctx.clb_nlist, atom_ctx.netlist(), pb_gpin_lookup);

        float worst_slack_95 = (float)summary.worst_slack_95;

        auto net_range = (opts_.v0_ordering_mode == "stable") ? vtr::make_range(stable_nets_.cbegin(), stable_nets_.cend()) : cluster_ctx.clb_nlist.nets();

        for (auto net_id : net_range) {
            float max_net_crit = 0.0f;
            
            auto pin_range = (opts_.v0_ordering_mode == "stable") ? vtr::make_range(stable_net_sinks_[size_t(net_id)].cbegin(), stable_net_sinks_[size_t(net_id)].cend()) : cluster_ctx.clb_nlist.net_sinks(net_id);
            
            for (auto pin_id : pin_range) {
                // Map ClusterPinId -> AtomPinId -> tNodeId
                auto atom_pins = pin_lookup.connected_atom_pins(pin_id);
                if (atom_pins.empty()) continue;
                
                AtomPinId atom_pin = *atom_pins.begin(); 
                tatum::NodeId node_id = atom_ctx.lookup().atom_pin_tnode(atom_pin);
                
                if (!node_id) continue;
                
                auto moments = fg.mu_var_S[size_t(node_id)];
                if (!moments.is_set()) continue;

                float slack95 = (float)(moments.mu - 1.64485 * std::sqrt(std::max(0.0, moments.var)));
                
                float risk = std::sqrt(std::max(0.0, moments.var)) / D_INTER; // Normalized by logic step
                float scaler = 1.0f + global_lambda * risk;
                
                // Crit95 calculation
                float crit95 = 0.0f;
                if (worst_slack_95 < 0.0f) {
                    crit95 = 1.0f - (slack95 / worst_slack_95);
                } else {
                    crit95 = (slack95 < 0.0f) ? 1.0f : 0.0f;
                }
                crit95 = std::max(0.0f, std::min(1.0f, crit95));

                float omega = crit95 * scaler;

                max_net_crit = std::max(max_net_crit, omega);
                
                ClusterBlockId blk = cluster_ctx.clb_nlist.pin_block(pin_id);
                block_crit_[size_t(blk)] = std::max(block_crit_[size_t(blk)], crit95);
            }
            net_omega_[size_t(net_id)] = max_net_crit;
        }
    }

    float get_block_crit(ClusterBlockId blk) {
        return block_crit_[size_t(blk)];
    }

    t_pl_loc find_legal_spiral(ClusterBlockId blk_id, t_pl_loc target) {
        auto& device_ctx = g_vpr_ctx.device();
        int max_dim = std::max(device_ctx.grid.width(), device_ctx.grid.height());

        // Config-specific salt for tile-selection tie-breaking.
        // When multiple tiles at the same radius are legal, we pick the one whose
        // position hash (XOR salt) sorts lowest — giving distinct selections per config.
        const bool do_salt = (opts_.v0_ordering_mode == "stable");
        const uint32_t tile_salt = do_salt
            ? (static_cast<uint32_t>(0x9e3779b9u) * static_cast<uint32_t>(opts_.prob_config_id + 1))
            : 0u;

        for (int r = 0; r < max_dim; ++r) {
            // Collect all candidate (legal) tiles at Chebyshev radius r
            struct Candidate {
                uint32_t sort_key;
                t_pl_loc loc;
            };
            std::vector<Candidate> candidates;

            for (int dx = -r; dx <= r; ++dx) {
                for (int dy = -r; dy <= r; ++dy) {
                    if (std::abs(dx) != r && std::abs(dy) != r) continue;

                    int x = target.x + dx;
                    int y = target.y + dy;

                    if (x < 0 || x >= (int)device_ctx.grid.width() ||
                        y < 0 || y >= (int)device_ctx.grid.height()) continue;

                    // [FIX] Skip non-root locations immediately.
                    // Multi-tile blocks must be placed at the root (offset 0,0).
                    if (!device_ctx.grid.is_root_location({x, y, 0})) continue;

                    auto type = device_ctx.grid.get_physical_type({x, y, 0});
                    for (int sub = 0; sub < type->capacity; ++sub) {
                        t_pl_loc loc(x, y, sub, 0);

                        t_pl_macro macro;
                        t_pl_macro_member member;
                        member.blk_index = blk_id;
                        member.offset = t_pl_offset(0, 0, 0, 0);
                        macro.members.push_back(member);

                        if (macro_can_be_placed(macro, loc, true, registry_)) {
                            // Salted sort key: FNV mix of (x,y,sub) XOR config salt
                            uint32_t h = 2166136261u;
                            auto mix = [&](int v) {
                                for (int i = 0; i < 4; ++i) {
                                    h ^= static_cast<uint8_t>(v >> (i * 8));
                                    h *= 16777619u;
                                }
                            };
                            mix(x); mix(y); mix(sub);
                            h ^= tile_salt;
                            candidates.push_back({h, loc});
                        }
                    }
                }
            }

            if (!candidates.empty()) {
                // Sort by salted key (stable, deterministic per config)
                std::sort(candidates.begin(), candidates.end(),
                          [](const Candidate& a, const Candidate& b) {
                    if (a.sort_key != b.sort_key) return a.sort_key < b.sort_key;
                    if (a.loc.x != b.loc.x) return a.loc.x < b.loc.x;
                    if (a.loc.y != b.loc.y) return a.loc.y < b.loc.y;
                    return a.loc.sub_tile < b.loc.sub_tile;
                });
                return candidates[0].loc;
            }
        }
        VPR_FATAL_ERROR(VPR_ERROR_PLACE, "V0: Could not find legal location for block %zu via spiral search!\n", size_t(blk_id));
        return target; 
    }


    t_pl_loc compute_barycenter(ClusterBlockId blk_id, int& empty_nets, int& zero_weight) {
        auto& cluster_ctx = g_vpr_ctx.clustering();
        auto& device_ctx = g_vpr_ctx.device();
        
        double sum_x = 0, sum_y = 0, sum_w = 0;

        auto bp_range = (opts_.v0_ordering_mode == "stable") ? vtr::make_range(stable_block_pins_[size_t(blk_id)].cbegin(), stable_block_pins_[size_t(blk_id)].cend()) : cluster_ctx.clb_nlist.block_pins(blk_id);

        for (auto pin_id : bp_range) {
            auto net_id = cluster_ctx.clb_nlist.pin_net(pin_id);
            float w = net_omega_[size_t(net_id)];
            
            // Barycenter of net
            double b_x = 0, b_y = 0;
            int anchor_count = 0;
            
            auto np_range = (opts_.v0_ordering_mode == "stable") ? vtr::make_range(stable_net_pins_[size_t(net_id)].cbegin(), stable_net_pins_[size_t(net_id)].cend()) : cluster_ctx.clb_nlist.net_pins(net_id);

            for (auto p : np_range) {
                ClusterBlockId b = cluster_ctx.clb_nlist.pin_block(p);
                if (fixed_blocks_.count(b) || placed_blocks_.count(b)) {
                    t_pl_loc l = registry_.block_locs()[b].loc;
                    b_x += l.x;
                    b_y += l.y;
                    anchor_count++;
                }
            }
            
            if (anchor_count > 0) {
                sum_x += (b_x / anchor_count) * w;
                sum_y += (b_y / anchor_count) * w;
                sum_w += w;
            } else {
                // Option B: Chip Center fallback for empty nets
                sum_x += (device_ctx.grid.width() / 2.0) * w;
                sum_y += (device_ctx.grid.height() / 2.0) * w;
                sum_w += w;
                empty_nets++;
            }
        }

        if (sum_w < 1e-9) {
            zero_weight++;
            // Deterministic Structural Fallback: Center + epsilon based on block ID
            int mid_x = device_ctx.grid.width() / 2;
            int mid_y = device_ctx.grid.height() / 2;
            return t_pl_loc(mid_x, mid_y, 0, 0);
        }

        return t_pl_loc(std::round(sum_x / sum_w), std::round(sum_y / sum_w), 0, 0);
    }

    void log_sensitivity_metrics() {
        if (net_omega_.empty()) return;

        double min_w = 1e20, max_w = -1e20, sum_w = 0, sum_w2 = 0;
        std::set<float> unique_w;
        for (float w : net_omega_) {
            min_w = std::min(min_w, (double)w);
            max_w = std::max(max_w, (double)w);
            sum_w += w;
            sum_w2 += (double)w * w;
            unique_w.insert(w);
        }
        double mean_w = sum_w / net_omega_.size();
        double std_w = std::sqrt(std::max(0.0, (sum_w2 / net_omega_.size()) - (mean_w * mean_w)));

        VTR_LOG("[V0_AUDIT] config_id: %d\n", opts_.prob_config_id);
        VTR_LOG("[V0_AUDIT] omega_min: %.6f\n", min_w);
        VTR_LOG("[V0_AUDIT] omega_max: %.6f\n", max_w);
        VTR_LOG("[V0_AUDIT] omega_mean: %.6f\n", mean_w);
        VTR_LOG("[V0_AUDIT] omega_std: %.6f\n", std_w);
        VTR_LOG("[V0_AUDIT] omega_unique_count: %zu\n", unique_w.size());

        // Log top 10 nets by omega
        VTR_LOG("[V0_AUDIT] Top-10 Net Weights:\n");
        std::vector<size_t> indices(net_omega_.size());
        std::iota(indices.begin(), indices.end(), 0);
        std::sort(indices.begin(), indices.end(), [this](size_t a, size_t b) { return net_omega_[a] > net_omega_[b]; });
        
        auto& cluster_ctx = g_vpr_ctx.clustering();
        for (int i = 0; i < std::min(10, (int)indices.size()); ++i) {
            auto net_id = ClusterNetId(indices[i]);
            int fanout = (int)cluster_ctx.clb_nlist.net_sinks(net_id).size();
            VTR_LOG("  [V0_AUDIT] Net %s: omega=%.4f fanout=%d\n", 
                   cluster_ctx.clb_nlist.net_name(net_id).c_str(), 
                   net_omega_[indices[i]], fanout);
        }
    }

    t_pl_loc find_legal_macro_spiral(const t_pl_macro& macro, t_pl_loc target_loc) {
        int max_radius = std::max(g_vpr_ctx.device().grid.width(), g_vpr_ctx.device().grid.height());
        for (int r = 0; r <= max_radius; r++) {
            for (int dx = -r; dx <= r; dx++) {
                for (int dy = -r; dy <= r; dy++) {
                    if (std::abs(dx) == r || std::abs(dy) == r) {
                        t_pl_loc test_loc = target_loc;
                        test_loc.x += dx;
                        test_loc.y += dy;
                        if (test_loc.x >= 0 && test_loc.x < (int)g_vpr_ctx.device().grid.width() &&
                            test_loc.y >= 0 && test_loc.y < (int)g_vpr_ctx.device().grid.height()) {
                            
                            // [FIX] Skip non-root locations immediately
                            if (!g_vpr_ctx.device().grid.is_root_location({test_loc.x, test_loc.y, test_loc.layer})) continue;

                            auto type = g_vpr_ctx.device().grid.get_physical_type({test_loc.x, test_loc.y, test_loc.layer});
                            int num_subtiles = type->capacity;
                            for (int st = 0; st < num_subtiles; st++) {
                                test_loc.sub_tile = st;
                                // Save current state in case it partially places then fails?
                                // try_place_macro does not pollute state if it fails.
                                if (try_place_macro(macro, test_loc, registry_)) {
                                    return test_loc;
                                }
                            }
                        }
                    }
                }
            }
        }
        VPR_FATAL_ERROR(VPR_ERROR_PLACE, "Could not find legal macro placement via spiral search!\n");
        return target_loc;
    }
};

void run_v0_placement(const t_placer_opts& placer_opts,
                      BlkLocRegistry& blk_loc_registry,
                      const PlaceMacros& place_macros) {
    V0Engine engine(placer_opts, blk_loc_registry, place_macros);
    engine.run();
}
