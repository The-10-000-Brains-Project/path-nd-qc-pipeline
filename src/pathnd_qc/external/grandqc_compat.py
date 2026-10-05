"""Compatibility bridge for the pinned GrandQC checkpoint format.

Copied beside managed upstream scripts; does not import the Path-ND QC package.
Only activated in the explicitly selected GrandQC subprocess.
"""
import importlib
import pkgutil
import sys
import types
import torch

def _bridge_before_load() -> None:
    """Expose the module paths required to unpickle the GrandQC checkpoint."""
    import timm.layers

    sys.modules.setdefault("timm.models.layers", timm.layers)
    for mod in pkgutil.iter_modules(timm.layers.__path__):
        try:
            sys.modules[f"timm.models.layers.{mod.name}"] = importlib.import_module(
                f"timm.layers.{mod.name}")
        except Exception:                      # noqa: BLE001 -- skip module paths absent from the installed dependency
            pass                               # is only a problem if the pickle needs it, and then
                                               # torch.load raises with the precise name.
    for old, new in (("timm.models.efficientnet_blocks", "timm.models._efficientnet_blocks"),
                     ("timm.models.efficientnet_builder", "timm.models._efficientnet_builder"),
                     ("timm.models.features", "timm.models._features")):
        try:
            sys.modules[old] = importlib.import_module(new)
        except Exception:                      # noqa: BLE001
            pass

    dec = importlib.import_module("segmentation_models_pytorch.decoders.unet.decoder")
    for old, new in (("DecoderBlock", "UnetDecoderBlock"), ("CenterBlock", "UnetCenterBlock")):
        if not hasattr(dec, old) and hasattr(dec, new):
            setattr(dec, old, getattr(dec, new))


def _bridge_after_load(model) -> dict:
    """Group B — rebind forwards that read attributes renamed between timm versions."""
    from timm.models._efficientnet_blocks import DepthwiseSeparableConv, InvertedResidual
    from segmentation_models_pytorch.decoders.unet.decoder import UnetDecoderBlock

    def _skip(mod) -> bool:                    # Map checkpoint has_residual to the has_skip attribute used by the loaded module.
        return bool(getattr(mod, "has_skip", getattr(mod, "has_residual", False)))

    def dsc_forward(self, x):
        shortcut = x
        x = self.bn1(self.conv_dw(x))
        if hasattr(self, "act1"):
            x = self.act1(x)
        x = self.bn2(self.conv_pw(self.se(x)))
        if hasattr(self, "act2"):
            x = self.act2(x)
        return x + shortcut if _skip(self) else x

    def ir_forward(self, x):
        shortcut = x
        x = self.bn1(self.conv_pw(x))
        if hasattr(self, "act1"):
            x = self.act1(x)
        x = self.bn2(self.conv_dw(x))
        if hasattr(self, "act2"):
            x = self.act2(x)
        x = self.bn3(self.conv_pwl(self.se(x)))
        if hasattr(self, "act3"):
            x = self.act3(x)
        return x + shortcut if _skip(self) else x

    counts = {"dsc": 0, "inverted_residual": 0, "decoder_block": 0}
    for mod in model.modules():
        if isinstance(mod, DepthwiseSeparableConv):
            mod.forward = types.MethodType(dsc_forward, mod); counts["dsc"] += 1
        elif isinstance(mod, InvertedResidual):
            mod.forward = types.MethodType(ir_forward, mod); counts["inverted_residual"] += 1
        elif isinstance(mod, UnetDecoderBlock):
            if not hasattr(mod, "interpolation_mode"):   # Supply the attribute that smp 0.5 forward() expects on the checkpoint object.
                mod.interpolation_mode = "nearest"       # predates the attribute
            counts["decoder_block"] += 1
    return counts



_bridge_before_load()
_original_load = torch.load

def _load(*args, **kwargs):
    kwargs.setdefault("weights_only", False)
    model = _original_load(*args, **kwargs)
    if isinstance(model, torch.nn.Module):
        _bridge_after_load(model)
    return model

torch.load = _load
