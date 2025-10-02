"""Utility helpers used by LLapDiT diffusion models."""

import math
from typing import List, Optional, Tuple

import torch
import torch.nn as nn


# ============================LLapDiT utils============================
def set_torch() -> torch.device:
    """Configure PyTorch defaults and return the active device.

    TF32 is enabled whenever CUDA is available and PyTorch exposes the relevant
    hooks.  The helper returns the device so callers can immediately cache it.
    """

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    cuda_backend = getattr(torch.backends, "cuda", None)
    if cuda_backend is not None and hasattr(cuda_backend, "is_built") and cuda_backend.is_built():
        cuda_backend.matmul.allow_tf32 = True
    if device.type == "cuda" and hasattr(torch, "set_float32_matmul_precision"):
        torch.set_float32_matmul_precision("high")
    return device


def sample_t_uniform(scheduler: "NoiseScheduler", n: int, device: torch.device) -> torch.Tensor:
    """Sample ``n`` discrete timesteps uniformly from ``[0, T)``."""

    return torch.randint(0, scheduler.timesteps, (n,), device=device)


def make_warmup_cosine(
    optimizer: torch.optim.Optimizer,
    total_steps: int,
    warmup_frac: float = 0.05,
    base_lr: float = 5e-4,
    min_lr: float = 1e-6,
) -> torch.optim.lr_scheduler.LambdaLR:
    """Return a cosine scheduler with linear warmup.

    Args:
        optimizer: Optimizer whose learning rate should be scheduled.
        total_steps: Total number of training steps.
        warmup_frac: Fraction of steps to spend in the warmup phase.
        base_lr: The learning rate reached after warmup.
        min_lr: Floor on the cosine annealed learning rate.
    """

    warmup_steps = max(1, int(total_steps * warmup_frac))

    def lr_lambda(step: int) -> float:
        if step < warmup_steps:
            return (step + 1) / max(1, warmup_steps)
        progress = (step - warmup_steps) / max(1, total_steps - warmup_steps)
        cosine = 0.5 * (1.0 + math.cos(math.pi * progress))
        floor = min_lr / max(base_lr, 1e-12)
        return max(floor, cosine)

    return torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda)


def _cosine_alpha_bar(ts: torch.Tensor, s: float = 0.008) -> torch.Tensor:
    """Cosine cumulative noise schedule from Nichol & Dhariwal (2021)."""

    return torch.cos(((ts + s) / (1.0 + s)) * math.pi * 0.5).pow(2)


class NoiseScheduler(nn.Module):
    """
    Diffusion utilities with precomputed buffers and a DDIM sampler.
    Supports 'linear' or 'cosine' schedules and epsilon-/v-/x0-parameterization.
    """

    def __init__(
        self,
        timesteps: int = 1000,
        schedule: str = "cosine",
        beta_start: float = 1e-4,
        beta_end: float = 2e-2,
    ) -> None:
        super().__init__()
        self.timesteps = int(timesteps)
        if schedule not in {"linear", "cosine"}:
            raise ValueError(f"Unknown schedule: {schedule}")
        self.schedule = schedule

        # ---- build betas ----
        if schedule == "linear":
            betas = torch.linspace(beta_start, beta_end, self.timesteps, dtype=torch.float32)
            betas = betas.clamp(min=1e-8, max=0.999)
            betas[0] = 0.0  # ensure ᾱ(0)=1 and no noise at t=0
        else:
            # cosine ᾱ(t): normalize so ᾱ(0)=1
            ts = torch.linspace(0.0, 1.0, self.timesteps + 1, dtype=torch.float32)
            abar = _cosine_alpha_bar(ts)
            abar = abar / abar[0].clamp_min(1e-12)
            # Convert ᾱ to per-step α_t: α_t = ᾱ_t / ᾱ_{t-1}
            ratios = (abar[1:] / abar[:-1]).clamp(1e-8, 0.999999)
            alphas = torch.ones(self.timesteps, dtype=torch.float32)
            if self.timesteps > 1:
                alphas[1:] = ratios[: self.timesteps - 1]
            betas = (1.0 - alphas)
            betas[0] = 0.0
            if self.timesteps > 1:
                betas[1:] = betas[1:].clamp(min=1e-8, max=0.999)

        # Register buffers
        self.register_buffer("betas", betas)  # [T]
        alphas = (1.0 - betas).clamp(1e-12, 1.0)  # [T]
        self.register_buffer("alphas", alphas)
        alpha_bars = torch.cumprod(alphas, dim=0)  # [T]
        self.register_buffer("alpha_bars", alpha_bars)

        # Precompute common roots
        ab = alpha_bars.clamp(0.0, 1.0)
        self.register_buffer("sqrt_alphas", torch.sqrt(alphas))
        self.register_buffer("sqrt_alpha_bars", torch.sqrt(ab))
        self.register_buffer(
            "sqrt_one_minus_alpha_bars",
            torch.sqrt((1.0 - ab).clamp(0.0, 1.0)),
        )

    @torch.no_grad()
    def timesteps_desc(self) -> torch.Tensor:
        """Return timesteps in reverse order as ``[T-1, ..., 0]``."""

        return torch.arange(
            self.timesteps - 1,
            -1,
            -1,
            dtype=torch.long,
            device=self.alpha_bars.device,
        )

    def _gather(self, buf: torch.Tensor, t: torch.Tensor) -> torch.Tensor:
        """Safely gather discrete values from a buffer for (possibly) float ``t``."""

        t_idx = t.clamp(min=0, max=self.timesteps - 1).to(device=buf.device, dtype=torch.long)
        return buf.gather(0, t_idx)

    def _expand_like(self, buf: torch.Tensor, t: torch.Tensor, ref: torch.Tensor) -> torch.Tensor:
        """Gather ``buf`` at ``t`` and reshape to broadcast with ``ref``."""

        return self._gather(buf, t).view(-1, *([1] * (ref.dim() - 1)))

    @torch.no_grad()
    def alpha_bar_at(self, t: torch.Tensor) -> torch.Tensor:
        """
        ᾱ(t) for possibly non-integer t in [0, T-1] via linear interpolation.
        Matches self.alpha_bars[t] exactly when t is integer.
        """
        t = t.to(self.alpha_bars.device, dtype=torch.float32)
        t0 = t.floor().clamp(0, self.timesteps - 1)
        t1 = (t0 + 1).clamp(0, self.timesteps - 1)
        w = (t - t0).clamp(0.0, 1.0)
        ab0 = self.alpha_bars.index_select(0, t0.long())
        ab1 = self.alpha_bars.index_select(0, t1.long())
        return (1.0 - w) * ab0 + w * ab1

    @torch.no_grad()
    def snr_at(self, t: torch.Tensor) -> torch.Tensor:
        abar = self.alpha_bar_at(t).clamp(1e-6, 1.0 - 1e-6)
        return abar / (1.0 - abar)

    def q_sample(
        self,
        x0: torch.Tensor,
        t: torch.Tensor,
        noise: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Sample ``x_t`` from the forward process and return the noise used."""

        if noise is None:
            noise = torch.randn_like(x0)
        sqrt_ab = self._expand_like(self.sqrt_alpha_bars, t, x0)
        sqrt_1_ab = self._expand_like(self.sqrt_one_minus_alpha_bars, t, x0)
        x_t = sqrt_ab * x0 + sqrt_1_ab * noise
        return x_t, noise  # noise is ε_true

    def pred_x0_from_eps(self, x_t: torch.Tensor, t: torch.Tensor, eps: torch.Tensor):
        alpha = self._expand_like(self.sqrt_alpha_bars, t, x_t)  # √ᾱ
        sigma = self._expand_like(self.sqrt_one_minus_alpha_bars, t, x_t)  # √(1-ᾱ)
        return (x_t - sigma * eps) / (alpha + 1e-12)

    def pred_eps_from_x0(self, x_t: torch.Tensor, t: torch.Tensor, x0: torch.Tensor):
        alpha = self._expand_like(self.sqrt_alpha_bars, t, x_t)
        sigma = self._expand_like(self.sqrt_one_minus_alpha_bars, t, x_t)
        return (x_t - alpha * x0) / (sigma + 1e-12)

    def pred_x0_from_v(self, x_t: torch.Tensor, t: torch.Tensor, v: torch.Tensor):
        # x0 = α x_t − σ v
        alpha = self._expand_like(self.sqrt_alpha_bars, t, x_t)
        sigma = self._expand_like(self.sqrt_one_minus_alpha_bars, t, x_t)
        return alpha * x_t - sigma * v

    def pred_eps_from_v(self, x_t: torch.Tensor, t: torch.Tensor, v: torch.Tensor):
        # ε = σ x_t + α v
        alpha = self._expand_like(self.sqrt_alpha_bars, t, x_t)
        sigma = self._expand_like(self.sqrt_one_minus_alpha_bars, t, x_t)
        return sigma * x_t + alpha * v

    def v_from_eps(self, x_t: torch.Tensor, t: torch.Tensor, eps: torch.Tensor):
        # v = (ε − σ x_t) / α
        alpha = self._expand_like(self.sqrt_alpha_bars, t, x_t)
        sigma = self._expand_like(self.sqrt_one_minus_alpha_bars, t, x_t)
        return (eps - sigma * x_t) / (alpha + 1e-12)

    def to_x0(self, x_t: torch.Tensor, t: torch.Tensor, pred: torch.Tensor, param_type: str):
        """Convert a model prediction to the ``x0`` parameterization."""

        if param_type == "eps":
            return self.pred_x0_from_eps(x_t, t, pred)
        elif param_type == "v":
            return self.pred_x0_from_v(x_t, t, pred)
        elif param_type == "x0":
            return pred
        else:
            raise ValueError("param_type must be 'eps', 'v', or 'x0'")

    def to_eps(self, x_t: torch.Tensor, t: torch.Tensor, pred: torch.Tensor, param_type: str):
        """Convert a model prediction to the ``eps`` parameterization."""

        if param_type == "eps":
            return pred
        elif param_type == "v":
            return self.pred_eps_from_v(x_t, t, pred)
        elif param_type == "x0":
            return self.pred_eps_from_x0(x_t, t, pred)
        else:
            raise ValueError("param_type must be 'eps', 'v', or 'x0'")

    @torch.no_grad()
    def ddim_sigma(self, t: torch.Tensor, t_prev: torch.Tensor, eta: float) -> torch.Tensor:
        ab_t = self._gather(self.alpha_bars, t).clamp(1e-12, 1.0)
        ab_prev = self._gather(self.alpha_bars, t_prev).clamp(1e-12, 1.0)
        sigma = (
            eta
            * torch.sqrt((1.0 - ab_prev) / (1.0 - ab_t))
            * torch.sqrt((1.0 - (ab_t / (ab_prev + 1e-12))).clamp_min(0.0))
        )
        return sigma.view(-1)

    @torch.no_grad()
    def ddim_step_from(
        self,
        x_t: torch.Tensor,
        t: torch.Tensor,
        t_prev: torch.Tensor,
        pred: torch.Tensor,
        param_type: str,
        eta: float = 0.0,
        noise: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        if noise is None:
            noise = torch.randn_like(x_t)

        ab_prev = self._expand_like(self.alpha_bars, t_prev, x_t).clamp(1e-12, 1.0)
        sigma = self.ddim_sigma(t, t_prev, eta).view(-1, *([1] * (x_t.dim() - 1)))

        x0_pred = self.to_x0(x_t, t, pred, param_type)
        eps_pred = self.to_eps(x_t, t, pred, param_type)

        dir_coeff = ((1.0 - ab_prev) - sigma ** 2).clamp_min(0.0)
        x_prev = torch.sqrt(ab_prev) * x0_pred + torch.sqrt(dir_coeff) * eps_pred + sigma * noise
        return x_prev


# ============================ Laplace pole logging ============================
def iter_laplace_bases(module: nn.Module):
    from Model.laptrans import LaplaceTransformEncoder

    for m in module.modules():
        if isinstance(m, LaplaceTransformEncoder):
            yield m


@torch.no_grad()
def log_pole_health(modules: List[nn.Module], log_fn, step: int, tag_prefix: str = ""):
    alphas, omegas = [], []
    for mod in modules:
        for lap in iter_laplace_bases(mod):
            tau = torch.nn.functional.softplus(lap._tau) + 1e-3
            alpha = lap.s_real.clamp_min(lap.alpha_min) * tau  # [k]
            omega = lap.s_imag * tau  # [k]
            alphas.append(alpha.detach().cpu())
            omegas.append(omega.detach().cpu())
    if not alphas:
        return
    alpha_cat = torch.cat([a.view(-1) for a in alphas])
    omega_cat = torch.cat([o.view(-1) for o in omegas])
    log_fn({f"{tag_prefix}alpha_mean": alpha_cat.mean().item(),
            f"{tag_prefix}alpha_min": alpha_cat.min().item(),
            f"{tag_prefix}alpha_max": alpha_cat.max().item(),
            f"{tag_prefix}omega_abs_mean": omega_cat.abs().mean().item()}, step=step)


def _print_log(metrics: dict, step: int, csv_path: str = None):
    msg = " | ".join(f"{k}={v:.4g}" for k, v in metrics.items())
    print(f"[poles] step {step:>7d} | {msg}")
    if csv_path is not None:
        import csv, os
        head = ["step"] + list(metrics.keys())
        row = [step] + [metrics[k] for k in metrics]
        write_header = not os.path.exists(csv_path)
        with open(csv_path, "a", newline="") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(head)
            w.writerow(row)


# ============================ VAE Latent stats helpers ============================
def flatten_targets(yb: torch.Tensor, mask_bn: torch.Tensor, device: torch.device) -> Tuple[torch.Tensor, torch.Tensor]:
    """yb: [B,N,H] -> y_in: [Beff,H,1], batch_ids: [Beff] mapping to B for cond rows"""
    y = yb.to(device)
    B, N, Hcur = y.shape
    y_flat = y.reshape(B * N, Hcur).unsqueeze(-1)  # [B*N, H, 1]
    m_flat = mask_bn.to(device).reshape(B * N)
    if not m_flat.any():
        return None, None
    y_in = y_flat[m_flat]
    batch_ids = torch.arange(B, device=device).unsqueeze(1).expand(B, N).reshape(B * N)[m_flat]
    return y_in, batch_ids


@torch.no_grad()
def _flatten_for_mask(yb, mask_bn, device):
    y_in, batch_ids = flatten_targets(yb, mask_bn, device)
    return y_in, batch_ids


# ============================ EMA Weights (for evaluation) ============================
class EMA:
    def __init__(self, model, decay=0.999):
        self.decay = decay
        self.shadow = {n: p.detach().clone() for n, p in model.named_parameters() if p.requires_grad}

    @torch.no_grad()
    def update(self, model):
        for n, p in model.named_parameters():
            if p.requires_grad:
                self.shadow[n].lerp_(p.detach(), 1.0 - self.decay)

    def store(self, model):
        self._backup = {n: p.detach().clone() for n, p in model.named_parameters() if p.requires_grad}

    def copy_to(self, model):
        for n, p in model.named_parameters():
            if p.requires_grad:
                p.data.copy_(self.shadow[n].data)

    def restore(self, model):
        for n, p in model.named_parameters():
            if p.requires_grad:
                p.data.copy_(self._backup[n].data)

    def state_dict(self):
        return {k: v.cpu() for k, v in self.shadow.items()}

    def load_state_dict(self, sd):
        for k, v in sd.items():
            if k in self.shadow:
                self.shadow[k] = v.clone()


# ============================ Latent helpers ============================
def simple_norm(mu: torch.Tensor, mu_mean: torch.Tensor, mu_std: torch.Tensor, clip_val = None) -> torch.Tensor:
    """
    Apply dataset-level per-dimension z-score to latent means.
      - mu: [B,L,Z]
      - mu_mean: [Z]
      - mu_std: [Z]
    Returns normalized latents with the same shape.
    """
    mu_mean = mu_mean.to(device=mu.device, dtype=mu.dtype).view(1,1,-1)
    mu_std  = mu_std.to(device=mu.device, dtype=mu.dtype).clamp_min(1e-6).view(1,1,-1)
    x = (mu - mu_mean) / mu_std
    if clip_val is not None:
        x = x.clamp(-clip_val, clip_val)
    return x


def invert_simple_norm(x: torch.Tensor, mu_mean: torch.Tensor, mu_std: torch.Tensor) -> torch.Tensor:
    """
    Inverse of simple_norm.
      - x: [B,L,Z]
    """
    mu_mean = mu_mean.to(device=x.device, dtype=x.dtype).view(1,1,-1)
    mu_std  = mu_std.to(device=x.device, dtype=x.dtype).view(1,1,-1)
    return x * mu_std + mu_mean


def normalize_cond_per_batch(cs: torch.Tensor, eps: float = 1e-6) -> torch.Tensor:
    """z-score over (B,S) for each feature dim; keeps gradients. cs: [B,S,Hc]"""
    m = cs.mean(dim=(0, 1), keepdim=True)
    v = cs.var(dim=(0, 1), keepdim=True, unbiased=False)
    return (cs - m) / (v.sqrt() + eps)


@torch.no_grad()
def compute_latent_stats(vae, dataloader, device):
    """
    Compute dataset-level latent mean/std on *raw* μ.
    """
    all_mu = []
    for (_, y, meta) in dataloader:
        m = meta["entity_mask"]
        B, N, H = y.shape
        y_flat = y.reshape(B * N, H).unsqueeze(-1)
        m_flat = m.reshape(B * N)
        if not m_flat.any():
            continue
        y_in = y_flat[m_flat].to(device)
        _, mu, _ = vae(y_in)  # [Beff, L, Z]
        all_mu.append(mu.detach().cpu())

    mu_cat = torch.cat(all_mu, dim=0)  # [sum(Beff), L, Z]
    mu_mean = mu_cat.mean(dim=(0, 1)).to(device)
    mu_std  = mu_cat.std(dim=(0, 1), correction=0).clamp_min(1e-6).to(device)
    return mu_mean, mu_std


def decode_latents_with_vae(vae, x0_norm: torch.Tensor,
                            mu_mean: torch.Tensor, mu_std: torch.Tensor) -> torch.Tensor:
    """
    Invert normalization and decode with the VAE decoder (no encoder skips).
      - x0_norm: [B,L,Z] normalized latent from diffusion
      - window_scale: None, scalar, [Z] or [B,1,Z]
    Returns:
      x_hat: [B,L,1]  (same layout your VAE was trained on)
    """
    mu_est = invert_simple_norm(x0_norm, mu_mean, mu_std)
    # your decoder accepts z with optional skips=None
    x_hat = vae.decoder(mu_est, encoder_skips=None)
    return x_hat


def build_context(context_module: nn.Module,
                  V,
                  T,
                  mask_bn,
                  device,
                  *,
                  norm: bool = True,
                  requires_grad: bool = False):
    """Returns normalized cond_summary: [B,S,Hm]"""
    series_diff = T.permute(0, 2, 1, 3).to(device)  # [B,K,N,F]
    series      = V.permute(0, 2, 1, 3).to(device)  # [B,K,N,F]
    mask_bn     = mask_bn.to(device)

    if context_module is None:
        raise AttributeError("context_module must be provided to build_context.")

    frozen = not any(p.requires_grad for p in context_module.parameters())
    grad_guard = torch.enable_grad if (requires_grad or not frozen) else torch.no_grad
    with grad_guard():
        cond_summary, _ = context_module(x=series, ctx_diff=series_diff, entity_mask=mask_bn)
    if norm:
        cond_summary = normalize_cond_per_batch(cond_summary)

    if not requires_grad:
        return cond_summary.detach()

    return cond_summary


def encode_mu_norm(vae, y_in: torch.Tensor, *,
                   mu_mean: torch.Tensor, mu_std: torch.Tensor) -> torch.Tensor:
    """VAE encode then globally z-score; returns [Beff, H, Z]"""
    with torch.no_grad():
        _, mu, _ = vae(y_in)
        mu_norm = simple_norm(mu, mu_mean, mu_std, clip_val=None)
        mu_norm = torch.nan_to_num(mu_norm, nan=0.0, posinf=0.0, neginf=0.0)
    return mu_norm


def diffusion_loss(
    model,
    scheduler,
    x0_lat_norm: torch.Tensor,
    t: torch.Tensor,
    *,
    cond_summary: Optional[torch.Tensor],
    predict_type: str = "v",                 # "v" or "eps"
    weight_scheme: str = "none",             # "none" or "weighted_min_snr"
    minsnr_gamma: float = 5.0,
    sc_feat: Optional[torch.Tensor] = None,
    reuse_xt_eps: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
) -> torch.Tensor:
    """
    MSE on v/eps with optional MinSNR weighting.

    Args:
        model: callable(x_t, t, cond_summary=..., sc_feat=...) -> pred
        scheduler: must provide q_sample(x0, t, noise) and v_from_eps(x_t, t, eps).
                   For MinSNR weighting, either:
                     - attribute `.alpha_bars` (1D tensor over discrete timesteps), or
                     - method `.alpha_bar_at(t)` for continuous/discrete `t`.
        x0_lat_norm: [B, ...] clean latents (normalized)
        t: [B] timesteps (int or float tensor)
        cond_summary: optional conditioning tensor
        predict_type: "v" (default) or "eps"
        weight_scheme: "none" (default) or "weighted_min_snr"
        minsnr_gamma: gamma for MinSNR
        sc_feat: optional side-channel features
        reuse_xt_eps: (x_t, eps_true) to skip resampling

    Returns:
        Scalar loss tensor.
    """
    # --- Forward diffusion (or reuse) ---
    if reuse_xt_eps is None:
        noise = torch.randn_like(x0_lat_norm)
        x_t, eps_true = scheduler.q_sample(x0_lat_norm, t, noise)
    else:
        x_t, eps_true = reuse_xt_eps

    # --- Prediction target ---
    pred = model(x_t, t, cond_summary=cond_summary, sc_feat=sc_feat)
    if predict_type == "eps":
        target = eps_true
    elif predict_type == "v":
        target = scheduler.v_from_eps(x_t, t, eps_true)
    else:
        raise ValueError(f"Unknown predict_type '{predict_type}'. Use 'v' or 'eps'.")

    # --- Per-sample MSE with full-mean reduction (resolution-invariant) ---
    err = (pred - target).pow(2)                        # [B, ...]
    reduce_dims = tuple(range(1, err.ndim))             # all non-batch dims
    per_sample = err.mean(dim=reduce_dims)              # [B]

    if weight_scheme == "none":
        return per_sample.mean()

    elif weight_scheme == "weighted_min_snr":
        # --- Compute alpha_bar for each sample ---
        # Prefer a scheduler method if available (supports continuous t)
        if hasattr(scheduler, "alpha_bar_at") and callable(getattr(scheduler, "alpha_bar_at")):
            abar = scheduler.alpha_bar_at(t)            # [B]
        elif hasattr(scheduler, "alpha_bars"):
            # Discrete timetable: gather per-sample by index
            # Ensure integer indices
            t_idx = t.long()
            alpha_bars = scheduler.alpha_bars           # [T] tensor
            if not torch.is_tensor(alpha_bars):
                raise TypeError("scheduler.alpha_bars must be a tensor for discrete indexing.")
            if t_idx.min() < 0 or t_idx.max() >= alpha_bars.numel():
                raise IndexError("t has indices outside the range of scheduler.alpha_bars.")
            abar = alpha_bars.to(x_t.device, x_t.dtype).index_select(0, t_idx)  # [B]
        else:
            raise AttributeError("Scheduler must provide alpha_bar_at(t) or alpha_bars.")

        # --- SNR and MinSNR weight ---
        abar = abar.clamp(1e-6, 1.0 - 1e-6)            # numerical stability
        snr = abar / (1.0 - abar)                       # [B]
        gamma = torch.as_tensor(minsnr_gamma, device=snr.device, dtype=snr.dtype)

        w = torch.minimum(snr, gamma) / (snr + 1.0)     # [B]
        w = w.detach()                                   # do not backprop through weights

        # --- Weight-normalized loss to keep scale stable across gamma choices ---
        w_mean = w.mean().clamp_min(1e-8)
        return (w * per_sample).mean() / w_mean

    else:
        raise ValueError(f"Unknown weight_scheme '{weight_scheme}'. Use 'none' or 'weighted_min_snr'.")


@torch.no_grad()
def calculate_v_variance(scheduler, dataloader, vae, device, latent_stats):
    """
    Calculates the variance of the v-prediction target over a given dataloader.
    """
    all_v_targets = []
    print("Calculating variance of v-prediction target...")

    # Unpack the pre-computed latent statistics
    mu_mean, mu_std = latent_stats

    # Loop through the validation set
    for xb, yb, meta in dataloader:
        # This block is the same as in your validate() function
        # It gets the normalized latent variable 'mu_norm' which is the x0 for diffusion
        y_in, _ = flatten_targets(yb, meta["entity_mask"], device)
        if y_in is None:
            continue

        mu_norm = encode_mu_norm(
            vae, y_in,
            mu_mean=mu_mean,
            mu_std=mu_std
        )

        # Now, simulate the process for creating the 'v' target
        # 1. Sample random timesteps
        t = sample_t_uniform(scheduler, mu_norm.size(0), device)

        # 2. Create the noise that would be added
        noise = torch.randn_like(mu_norm)

        # 3. Apply the forward process to get the noised latent x_t
        x_t, _ = scheduler.q_sample(mu_norm, t, noise)

        # 4. Calculate the ground-truth 'v' from x_t and the noise
        v_target = scheduler.v_from_eps(x_t, t, noise)
        all_v_targets.append(v_target.detach().cpu())

    # Concatenate all batches and compute the final variance
    if not all_v_targets:
        print("Warning: No valid data found to calculate variance.")
        return float('nan')

    all_v_targets_cat = torch.cat(all_v_targets, dim=0)
    v_variance = all_v_targets_cat.var(correction=0).item()

    return v_variance
