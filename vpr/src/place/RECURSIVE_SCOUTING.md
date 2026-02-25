# Recursive Scouting & Autonomous VPR (v7.0 Grand Encyclopedia)

This document preserves the mathematical, architectural, and operational knowledge of the Recursive Scouting placement engine.

## 🏛️ 1. Core Philosophy: Beyond Stochastic Search

Traditional VPR treats placement as a **Simulated Annealing Heuristic**—a greedy stochastic walk through a move-space. Recursive Scouting transforms this into a **Probabilistic Inference Problem**. Instead of one trajectory, the engine launches an **Evolutionary Portfolio of 16 Scouts**, each probing a different "Physical Reality" (Hyperparameter Region). The **Factor Graph** then infers the reality most likely to yield an unconstrained global minimum.

## ⚛️ 2. Mathematical Foundations: Factor Graph Mode 4

The engine uses a Gaussian Message Passing framework to model timing uncertainty.

### 2.1 The Gaussian Framework
For any node $v$, arrival time $T_v \sim \mathcal{N}(\mu_v, \sigma_v^2)$:
- **Mean ($\mu_v$)**: Maximum Likelihood arrival time (deterministic STA).
- **Variance ($\sigma_v^2$)**: Systemic Uncertainty / Look-Ahead potential.

### 2.2 Reconvergence: Clark’s Approximation
During max-factor propagation ($Z = \max(X, Y)$), we use Clark’s Approximation to "smear" criticality across multiple paths, preventing the placer from burying near-critical paths.

## 💻 3. Code-Level Integration: The ADC Engine

Integrated primarily in `place_timing_update.cpp`.

### 3.1 Adaptive Differential Criticality (ADC)
The cost function is augmented with an Inference Correction:
$$\text{Crit}_{eff} = \text{Crit}_{det} + \lambda_{eff} \cdot (\text{Crit}_{prob} - \text{Crit}_{det}) \cdot \omega_{fanout}$$
- **$\lambda_{eff}$**: Injection pressure.
- **$\omega_{fanout}$**: Fanout Dampening ($1 / (1 + \log(\text{pin\_count}))$).

### 3.2 Success-Aware Momentum
Managed by `g_adaptive_momentum_scaler`. Momentum boosts on detected gains (up to 5x) and decays on regressions.

### 3.3 Topological Resolver
Self-calibrates before step 0:
- $\alpha = 0.6 \cdot R + 0.02$ (Reconvergence Density $R$)
- $\lambda = 0.001 \cdot D + 0.04$ (Median Depth $D$)

## 📊 4. The Scouting Tournament

1. **Stage 1 (Portfolio Search)**: 16 Scouts with different $(\lambda, \alpha, \beta, \gamma)$ presets.
2. **Tiered Pruning**:
   - Step 15: Prune to 8
   - Step 45: Prune to 4
   - Step 80: Select Golden Winner
3. **Stage 2 (Mass Exploitation)**: Run the winners with 16 random seeds.
4. **Selection**: Driven by $PBScore = \mu_{slack} - 1.645 \cdot \sigma_{sigma}$.

## ⌨️ 5. Operational Manual

### Build Requirements
Build in **Release Mode** for efficiency.
```bash
cmake .. -DCMAKE_BUILD_TYPE=Release
make vpr -j$(nproc)
```

### CLI Flags
- `--autonomous`: Enable the tournament.
- `--prob_timing_mode 4`: Use Factor Graph.
- `--prob_self_calibrate`: Enable topological resolver.
- `--prob_congestion_gamma`: Control structural spreading.

### Success Signal (Log Signature)
`AUTONOMOUS_ENGINE: Resolved structure R=0.18 D=12.5 -> α=0.1280 λ=0.0525`
`!!! ADAPTIVE_P5.1_ACTIVATE !!! total_diff=45.2 max_diff=0.88 momentum=1.40`
