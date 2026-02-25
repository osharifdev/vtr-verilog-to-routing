#ifndef AUTONOMOUS_ENGINE_H
#define AUTONOMOUS_ENGINE_H

#include <vector>
#include <string>

struct t_autonomous_config {
    int id;
    float lambda;
    float beta;
    float alpha;
    float gate;
    float boost;
};

// The "Elite 16" Hero Portfolio (Re-aligned with Python Audit V5)
const std::vector<t_autonomous_config> ELITE_16_PORTFOLIO = {
    {30, 0.050f, 1.0f, 0.10f, 0.000f, 1.1f}, 
    {5,  0.100f, 1.0f, 0.10f, 5.0e-10f, 1.1f},
    {31, 0.200f, 1.0f, 0.10f, 0.000f, 1.1f},
    {4,  0.050f, 1.0f, 0.10f, 5.0e-10f, 1.1f},
    {6,  0.200f, 1.0f, 0.10f, 5.0e-10f, 1.1f},
    {20, 0.005f, 2.0f, 0.05f, 5.0e-10f, 1.1f}, // mkPktMerge Precision Recipe
    {29, 1.000f, 1.0f, 0.10f, 5.0e-10f, 1.1f}, // bgm Breakout
    {2,  0.010f, 1.0f, 0.10f, 5.0e-10f, 1.1f},
    {3,  0.020f, 1.0f, 0.10f, 5.0e-10f, 1.1f},
    {22, 0.050f, 2.0f, 0.05f, 5.0e-10f, 1.1f},
    {18, 0.200f, 1.0f, 0.05f, 5.0e-10f, 1.1f},
    {15, 0.005f, 1.0f, 0.20f, 5.0e-10f, 1.1f},
    {9,  0.005f, 2.0f, 0.10f, 5.0e-10f, 1.1f},
    {14, 0.005f, 1.0f, 0.05f, 5.0e-10f, 1.1f},
    {28, 0.500f, 2.0f, 0.05f, 0.000f, 1.1f},
    {7,  0.500f, 1.0f, 0.10f, 5.0e-10f, 1.1f}
};

struct t_tournament_result {
    t_tournament_result() : config_id(0), seed(0), cost(0.0), cpd(1e20), pb_score(0.0), current_step(0), is_terminated(false), success(false) {}
    t_tournament_result(int id, int s, double p, double c, double score, bool succ) noexcept
        : config_id(id), seed(s), cost(p), cpd(c), pb_score(score), current_step(0), is_terminated(false), success(succ) {}
    int config_id;
    int seed;
    double cost;
    double cpd;
    double pb_score;
    int current_step;
    bool is_terminated;
    bool success;
};

#endif
