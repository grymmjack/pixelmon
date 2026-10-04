"""Seamless (tileable) generation: circular padding for the UNet and the VAE decoder.

SDXL's convolutions zero-pad at the image border, so every render has hard edges and never tiles,
whatever the prompt says. Swapping that padding for wrap-around ("circular") makes the left edge
continue into the right one (and the top into the bottom), so the picture repeats with no seam.

The cached checkpoint is never modified for good: the UNet is patched only for the duration of each
sampling step (a model_function_wrapper on a cloned ModelPatcher), and the VAE only while it decodes.
"""
import types

import torch
import torch.nn.functional as F

TILE_MODES = ["both", "x", "y"]


def _circular_conv_forward(wrap_x, wrap_y):
    def _conv_forward(self, input, weight, bias):
        ph, pw = self.padding
        # x first (last dim), then y; an axis that doesn't wrap keeps the usual zero padding
        input = F.pad(input, (pw, pw, 0, 0), mode="circular" if wrap_x else "constant")
        input = F.pad(input, (0, 0, ph, ph), mode="circular" if wrap_y else "constant")
        return F.conv2d(input, weight, bias, self.stride, 0, self.dilation, self.groups)
    return _conv_forward


def _convs(module):
    """Every 2D conv that pads its input (the ones whose border handling makes the seam)."""
    return [m for m in module.modules()
            if isinstance(m, torch.nn.Conv2d) and isinstance(m.padding, tuple) and any(m.padding)
            and m.padding_mode == "zeros"]


class _Circular:
    """Context manager: circular padding on `convs` while inside, the original behaviour after."""

    def __init__(self, convs, mode):
        self.convs, self.fwd = convs, _circular_conv_forward(mode in ("both", "x"), mode in ("both", "y"))

    def __enter__(self):
        for c in self.convs:
            c._conv_forward = types.MethodType(self.fwd, c)   # instance attr shadows the class method

    def __exit__(self, *exc):
        for c in self.convs:
            c.__dict__.pop("_conv_forward", None)


class SeamlessTile:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"model": ("MODEL",), "tiling": (TILE_MODES, {"default": "both"})}}

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "patch"
    CATEGORY = "image/pixel art"

    def patch(self, model, tiling):
        m = model.clone()
        convs = _convs(m.model.diffusion_model)
        prev = m.model_options.get("model_function_wrapper")

        def wrapper(apply_model, args):
            with _Circular(convs, tiling):
                if prev is not None:
                    return prev(apply_model, args)
                return apply_model(args["input"], args["timestep"], **args["c"])

        m.set_model_unet_function_wrapper(wrapper)
        return (m,)


class CircularVAEDecode:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"samples": ("LATENT",), "vae": ("VAE",),
                             "tiling": (TILE_MODES, {"default": "both"})}}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "decode"
    CATEGORY = "image/pixel art"

    def decode(self, samples, vae, tiling):
        with _Circular(_convs(vae.first_stage_model), tiling):
            images = vae.decode(samples["samples"])
        if images.ndim == 5:   # video VAEs return (B, T, H, W, C)
            images = images.reshape(-1, *images.shape[-3:])
        return (images,)


class TilePreview:
    """The image repeated across x n grid, to eyeball whether a texture's seams really vanish."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"image": ("IMAGE",), "repeat": ("INT", {"default": 3, "min": 2, "max": 8}),
                             "tiling": (TILE_MODES, {"default": "both"})}}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "tile"
    CATEGORY = "image/pixel art"

    def tile(self, image, repeat, tiling):
        ry = repeat if tiling in ("both", "y") else 1
        rx = repeat if tiling in ("both", "x") else 1
        return (image.repeat(1, ry, rx, 1),)
