#pragma once
#include <string>
#include "clustered_netlist_fwd.h"

class PlacerState;
struct PlaceCritParams;
class PlacerCriticalities;
class PlacerSetupSlacks;
class NetPinTimingInvalidator;
class PlaceDelayModel;
class SetupTimingInfo;
struct t_placer_opts;
class t_placer_costs;

///@brief Initialize the timing information and structures in the placer.
void initialize_timing_info(const t_placer_opts& placer_opts,
                            const PlaceCritParams& crit_params,
                            const PlaceDelayModel* delay_model,
                            PlacerCriticalities* criticalities,
                            PlacerSetupSlacks* setup_slacks,
                            NetPinTimingInvalidator* pin_timing_invalidator,
                            SetupTimingInfo* timing_info,
                            t_placer_costs* costs,
                            PlacerState& placer_state);

///@brief Updates every timing related classes, variables and structures.
void perform_full_timing_update(const t_placer_opts& placer_opts,
                                const PlaceCritParams& crit_params,
                                const PlaceDelayModel* delay_model,
                                PlacerCriticalities* criticalities,
                                PlacerSetupSlacks* setup_slacks,
                                NetPinTimingInvalidator* pin_timing_invalidator,
                                SetupTimingInfo* timing_info,
                                t_placer_costs* costs,
                                PlacerState& placer_state);

void finish_prob_inject_step1_audit(const std::string& circuit_name, int seed, SetupTimingInfo* timing_info);

///@brief Update timing information based on the current block positions.
void update_timing_classes(const PlaceCritParams& crit_params,
                           SetupTimingInfo* timing_info,
                           PlacerCriticalities* criticalities,
                           PlacerSetupSlacks* setup_slacks,
                           NetPinTimingInvalidator* pin_timing_invalidator);

///@brief Updates the timing driven (td) costs.
void update_timing_cost(const PlaceDelayModel* delay_model,
                        const PlacerCriticalities* criticalities,
                        PlacerState& placer_state,
                        double* timing_cost);

///@brief Incrementally updates timing cost based on the current delays and criticality estimates.
void update_td_costs(const PlaceDelayModel* delay_model,
                     const PlacerCriticalities& place_crit,
                     PlacerState& placer_state,
                     double* timing_cost);

///@brief Recomputes timing cost from scratch based on the current delays and criticality estimates.
void comp_td_costs(const PlaceDelayModel* delay_model,
                   const PlacerCriticalities& place_crit,
                   PlacerState& placer_state,
                   double* timing_cost);

double comp_td_connection_cost(const PlaceDelayModel* delay_model,
                               const PlacerCriticalities& place_crit,
                               const PlacerState& placer_state,
                               ClusterNetId net,
                               int ipin);

/**
 * @brief Commit all the setup slack values from the PlacerSetupSlacks
 *        class to `connection_setup_slack`.
 */
void commit_setup_slacks(const PlacerSetupSlacks* setup_slacks,
                         PlacerState& placer_state);

///@brief Verify that the values in `connection_setup_slack` matches PlacerSetupSlacks.
bool verify_connection_setup_slacks(const PlacerSetupSlacks* setup_slacks,
                                    const PlacerState& placer_state);

/**
 * @brief Emits factor-graph proxy checkpoint metrics at early annealing iterations.
 *
 * Records mean criticality uplift, activated mass, reconvergence pressure,
 * tight competition fraction, uplift drift, normalization context, and sanity
 * checks at the specified annealing iteration. Appends one row to the CSV at
 * output_path.
 *
 * Must be called AFTER perform_full_timing_update() so that g_fg_view and
 * g_prob_differential_crit are populated.
 */
void emit_factor_graph_proxy_checkpoint(
    int iteration,
    SetupTimingInfo* timing_info,
    const PlacerCriticalities* criticalities,
    const PlacerState& placer_state,
    const t_placer_opts& placer_opts,
    const std::string& output_path,
    double bb_cost = 0.0,
    double timing_cost = 0.0,
    double total_cost = 0.0,
    double bb_cost_norm = 0.0,
    double timing_cost_norm = 0.0);

/**
 * @brief Emits a geometry-only proxy checkpoint (PROXY 1.2/1.3).
 *
 * Computes only placement geometry features (net HPWL statistics)
 * without requiring timing information, delay models, or factor graphs.
 * All timing/FG columns are written as 0.
 */
void emit_geometry_only_proxy_checkpoint(
    int iteration,
    const PlacerState& placer_state,
    const std::string& output_path,
    double bb_cost = 0.0);
