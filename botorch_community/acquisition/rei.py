#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

r"""
Region Averaged Acquisition Functions for Efficient Trust Region Selection
for High-Dimensional Trust Region Bayesian Optimization.
See [eriksson2019TuRBO]_, [namura2024rei]_

Two versions of the Regional Expected Improvement (REI) acquisition
function are implemented here:
1. Analytic version: LogRegionalExpectedImprovement
2. Monte Carlo version: qLogRegionalExpectedImprovement

LogREI has been implemented from the original paper [namura2024rei]_
and qLogREI have been implemented by slightly modifying the qREI
acquisition function implementation from the paper.

These acquisition functions can help explore the design space efficiently
since trust regions at initialization and restarts in algorithms like TuRBO
are selected by optimizing the Region Averaged Acquisition Functions instead
of sampling them randomly. This has displayed faster convergence in some cases
and convergence to better solutions in general as showed in [namura2024rei]_.

References

.. [eriksson2019TuRBO]
    D. Eriksson, M. Pearce, J.R. Gardner, R. Turner, M. Poloczek.
    Scalable Global Optimization via Local Bayesian Optimization.
    Advances in Neural Information Processing Systems, 2019.
.. [namura2024rei]
    Nobuo Namura, Sho Takemori.
    Regional Expected Improvement for Efficient Trust Region
    Selection in High-Dimensional Bayesian Optimization.
    Proceedings of the 39th AAAI Conference on Artificial
    Intelligence, 2025.

Contributor: SaiAakash
"""

from __future__ import annotations

import torch
from botorch.acquisition.analytic import (
    _log_ei_helper,
    _scaled_improvement,
    AnalyticAcquisitionFunction,
)
from botorch.acquisition.logei import _log_improvement, check_tau
from botorch.acquisition.monte_carlo import MCAcquisitionFunction
from botorch.acquisition.objective import MCAcquisitionObjective, PosteriorTransform
from botorch.models.model import Model
from botorch.sampling.base import MCSampler
from botorch.utils.safe_math import logmeanexp
from botorch.utils.transforms import concatenate_pending_points, t_batch_mode_transform
from torch import Tensor

TAU_RELU = 1e-6
TAU_MAX = 1e-2


class LogRegionalExpectedImprovement(AnalyticAcquisitionFunction):
    _log: bool = True

    def __init__(
        self,
        model: Model,
        best_f: float | Tensor,
        X_dev: float | Tensor,
        posterior_transform: PosteriorTransform | None = None,
        maximize: bool = True,
        length: float = 0.8,
        bounds: float | Tensor | None = None,
    ) -> None:
        r"""Log-Regional Expected Improvement (analytic).

        Args:
            model: A fitted single-outcome model.
            best_f: Either a scalar or a `b`-dim Tensor (batch mode) representing
                the best function value observed so far (assumed noiseless).
            X_dev: A `n x d`-dim Tensor of `n` `d`-dim design points within a TR.
            posterior_transform: A PosteriorTransform. If using a multi-output model,
                a PosteriorTransform that transforms the multi-output posterior into a
                single-output posterior is required.
            maximize: If True, consider the problem a maximization problem.
            length: The length of the trust region to consider.
            bounds: The bounds of the design space.
                First column represents dimension-wise lower bounds.
                Second column represents dimension-wise upper bounds.
        """

        super().__init__(model=model, posterior_transform=posterior_transform)
        self.register_buffer("best_f", torch.as_tensor(best_f))
        self.maximize: bool = maximize

        dim: int = X_dev.shape[1]
        self.n_region: int = X_dev.shape[0]
        self.X_dev: Tensor = X_dev.reshape(self.n_region, 1, 1, -1)
        self.length: float = length
        if bounds is not None:
            self.bounds = bounds
        else:
            self.bounds = torch.stack([torch.zeros(dim), torch.ones(dim)]).to(
                device=self.X_dev.device, dtype=self.X_dev.dtype
            )

    @t_batch_mode_transform(expected_q=1)
    def forward(self, X: Tensor) -> Tensor:
        batch_shape = X.shape[0]
        q = X.shape[1]
        d = X.shape[2]

        # make N_x samples in design space
        X_min = (X - 0.5 * self.length).clamp_min(self.bounds[0]).unsqueeze(0)
        X_max = (X + 0.5 * self.length).clamp_max(self.bounds[1]).unsqueeze(0)
        Xs = (self.X_dev * (X_max - X_min) + X_min).reshape(-1, q, d)

        mean, sigma = self._mean_and_sigma(Xs)
        u = _scaled_improvement(mean, sigma, self.best_f, self.maximize)
        logei = _log_ei_helper(u) + sigma.log()
        logrei = logmeanexp(logei.reshape(self.n_region, batch_shape), dim=0)
        return logrei


class qLogRegionalExpectedImprovement(MCAcquisitionFunction):
    def __init__(
        self,
        model: Model,
        best_f: float | Tensor,
        X_dev: float | Tensor,
        sampler: MCSampler | None = None,
        objective: MCAcquisitionObjective | None = None,
        posterior_transform: PosteriorTransform | None = None,
        X_pending: Tensor | None = None,
        length: float = 0.8,
        bounds: float | Tensor | None = None,
        fat: bool = True,
        tau_relu: float = TAU_RELU,
    ) -> None:
        r"""q-Log Regional Expected Improvement (MC acquisition function).

        Args:
            model: A fitted single-outcome model.
            best_f: Either a scalar or a `b`-dim Tensor (batch mode) representing
                the best function value observed so far (assumed noiseless).
            X_dev: A `n x d`-dim Tensor of `n` `d`-dim design points within a TR.
            sampler: botorch.sampling.base.MCSampler
                The sampler used to sample fantasized models. Defaults to
                SobolQMCNormalSampler(num_samples=1)`.
            objective: The MCAcquisitionObjective under which the samples are evaluated.
                Defaults to `IdentityMCObjective()`.
                NOTE: `ConstrainedMCObjective` for outcome constraints is deprecated in
                favor of passing the `constraints` directly to this constructor.
            posterior_transform: A PosteriorTransform. If using a multi-output model,
                a PosteriorTransform that transforms the multi-output posterior into a
                single-output posterior is required.
            X_pending: A `batch_shape x m x d`-dim Tensor of `m` `d`-dim design
                points that have been submitted for function evaluation but have
                not yet been evaluated.
            length: The length of the trust region to consider.
            bounds: The bounds of the design space.
                First column represents dimension-wise lower bounds.
                Second column represents dimension-wise upper bounds.
            fat: Toggles the logarithmic / linear asymptotic behavior of the smooth
                approximation to the ReLU.
            tau_relu: Temperature parameter controlling the sharpness of the smooth
                approximations to ReLU.

        """
        super().__init__(
            model=model,
            sampler=sampler,
            objective=objective,
            posterior_transform=posterior_transform,
            X_pending=X_pending,
        )
        self.register_buffer("best_f", torch.as_tensor(best_f, dtype=float))
        self.fat: bool = fat
        self.tau_relu: float = check_tau(tau_relu, "tau_relu")
        dim: int = X_dev.shape[1]
        self.n_region: int = X_dev.shape[0]
        self.X_dev: Tensor = X_dev.reshape(self.n_region, 1, 1, -1)
        self.length: float = length
        if bounds is not None:
            self.bounds = bounds
        else:
            self.bounds = torch.stack([torch.zeros(dim), torch.ones(dim)]).to(
                device=self.X_dev.device, dtype=self.X_dev.dtype
            )

    @concatenate_pending_points
    @t_batch_mode_transform()
    def forward(self, X: Tensor) -> Tensor:
        batch_shape = X.shape[0]
        q = X.shape[1]
        d = X.shape[2]

        # make N_x samples in design space
        X_min = (X - 0.5 * self.length).clamp_min(self.bounds[0]).unsqueeze(0)
        X_max = (X + 0.5 * self.length).clamp_max(self.bounds[1]).unsqueeze(0)
        Xs = (self.X_dev * (X_max - X_min) + X_min).reshape(-1, q, d)

        posterior = self.model.posterior(
            X=Xs, posterior_transform=self.posterior_transform
        )
        samples = self.get_posterior_samples(posterior)
        obj = self.objective(samples, X=Xs)
        obj = _log_improvement(obj, self.best_f, self.tau_relu, self.fat).reshape(
            -1, self.n_region, batch_shape, q
        )
        q_log_rei = obj.max(dim=-1)[0].mean(dim=(0, 1))

        return q_log_rei
