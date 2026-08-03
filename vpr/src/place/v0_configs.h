#ifndef V0_CONFIGS_H
#define V0_CONFIGS_H

#include <array>

/**
 * @brief Represents a probabilistic configuration for VPR placement.
 * 
 * These parameters drive the variance propagation in the Factor Graph
 * and shift the inferred timing pressure.
 */
struct ProbConfig {
    int id;
    float lambda; // Diffusion/Injection strength
    float beta;   // Spatial penalty factor
    float alpha;  // Delay variance scaling
    float gate;   // Slack-gating threshold
    float boost;  // Success momentum boost
};

/**
 * @brief The source-of-truth portfolio for V0 sweeps.
 * 
 * Contains 16 original configurations and 8 K6-tuned configurations.
 */
static const std::array<ProbConfig, 24> kProbConfigs = {{
    // ── Original 16 (tuned for unmapped 4-LUT circuits, D~12, R~0.22) ──
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
    {7,  0.500f, 1.0f, 0.10f, 5.0e-10f, 1.1f},
    // ── K6-tuned additions (tamed λ, high-β, for shallow D~8 topology) ──
    {40, 0.100f, 2.0f, 0.05f, 0.000f, 1.2f}, // K6-light pressure
    {41, 0.200f, 2.0f, 0.05f, 0.000f, 1.2f}, // K6-medium pressure
    {42, 0.300f, 2.0f, 0.05f, 0.000f, 1.2f}, // K6-strong pressure
    {43, 0.400f, 2.0f, 0.05f, 0.000f, 1.2f}, // K6-hammer
    {44, 0.100f, 3.0f, 0.03f, 0.000f, 1.2f}, // K6-wide uncertainty
    {45, 0.200f, 3.0f, 0.03f, 0.000f, 1.2f}, // K6-wide+medium
    {46, 0.300f, 3.0f, 0.03f, 0.000f, 1.2f}, // K6-wide+strong
    {47, 0.100f, 2.0f, 0.10f, 0.000f, 1.3f}, // K6-boosted momentum
}};

#endif
