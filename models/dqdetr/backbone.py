# ------------------------------------------------------------------------
# DINO
# Copyright (c) 2022 IDEA. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 [see LICENSE for details]
# ------------------------------------------------------------------------
# Conditional DETR
# Copyright (c) 2021 Microsoft. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 [see LICENSE for details]
# ------------------------------------------------------------------------
# Copied from DETR (https://github.com/facebookresearch/detr)
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
# ------------------------------------------------------------------------

"""
Backbone modules.
"""
from collections import OrderedDict
import os

import torch
import torch.nn.functional as F
import torchvision
from torch import nn
from torchvision.models._utils import IntermediateLayerGetter
from typing import Dict, List

from .SSLVisionTransformer import SSLVisionTransformer
from util.misc import NestedTensor, clean_state_dict, is_main_process
from .position_encoding import build_position_encoding




class FrozenBatchNorm2d(torch.nn.Module):
    """
    BatchNorm2d where the batch statistics and the affine parameters are fixed.

    Copy-paste from torchvision.misc.ops with added eps before rqsrt,
    without which any other models than torchvision.models.resnet[18,34,50,101]
    produce nans.
    """

    def __init__(self, n):
        super(FrozenBatchNorm2d, self).__init__()
        self.register_buffer("weight", torch.ones(n))
        self.register_buffer("bias", torch.zeros(n))
        self.register_buffer("running_mean", torch.zeros(n))
        self.register_buffer("running_var", torch.ones(n))

    def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict,
                              missing_keys, unexpected_keys, error_msgs):
        num_batches_tracked_key = prefix + 'num_batches_tracked'
        if num_batches_tracked_key in state_dict:
            del state_dict[num_batches_tracked_key]

        super(FrozenBatchNorm2d, self)._load_from_state_dict(
            state_dict, prefix, local_metadata, strict,
            missing_keys, unexpected_keys, error_msgs)

    def forward(self, x):
        # move reshapes to the beginning
        # to make it fuser-friendly
        w = self.weight.reshape(1, -1, 1, 1)
        b = self.bias.reshape(1, -1, 1, 1)
        rv = self.running_var.reshape(1, -1, 1, 1)
        rm = self.running_mean.reshape(1, -1, 1, 1)
        eps = 1e-5
        scale = w * (rv + eps).rsqrt()
        bias = b - rm * scale
        return x * scale + bias


class BackboneBase(nn.Module):

    def __init__(self, backbone: nn.Module, train_backbone: bool, num_channels: int, return_interm_indices: list):
        super().__init__()
        for name, parameter in backbone.named_parameters():
            if not train_backbone or 'layer2' not in name and 'layer3' not in name and 'layer4' not in name:
                parameter.requires_grad_(False)

        return_layers = {}
        for idx, layer_index in enumerate(return_interm_indices):
            return_layers.update({"layer{}".format(5 - len(return_interm_indices) + idx): "{}".format(layer_index)})

        # if len:
        #     if use_stage1_feature:
        #         return_layers = {"layer1": "0", "layer2": "1", "layer3": "2", "layer4": "3"}
        #     else:
        #         return_layers = {"layer2": "0", "layer3": "1", "layer4": "2"}
        # else:
        #     return_layers = {'layer4': "0"}
        self.body = IntermediateLayerGetter(backbone, return_layers=return_layers)
        self.num_channels = num_channels

    def forward(self, tensor_list: NestedTensor):
        xs = self.body(tensor_list.tensors)
        out: Dict[str, NestedTensor] = {}
        for name, x in xs.items():
            m = tensor_list.mask
            assert m is not None
            mask = F.interpolate(m[None].float(), size=x.shape[-2:]).to(torch.bool)[0]
            out[name] = NestedTensor(x, mask)

        return out


class Backbone(BackboneBase):
    """ResNet backbone with frozen BatchNorm."""
    def __init__(self, name: str,
                 train_backbone: bool,
                 dilation: bool,
                 return_interm_indices:list,
                 batch_norm=FrozenBatchNorm2d,
                 ):
        if name in ['resnet18', 'resnet34', 'resnet50', 'resnet101']:
            backbone = getattr(torchvision.models, name)(
                replace_stride_with_dilation=[False, False, dilation],
                pretrained=is_main_process(), norm_layer=batch_norm)
        else:
            raise NotImplementedError("Why you can get here with name {}".format(name))
        
        #print("------------------------------------------------")
        #print(is_main_process())
        # num_channels = 512 if name in ('resnet18', 'resnet34') else 2048
        assert name not in ('resnet18', 'resnet34'), "Only resnet50 and resnet101 are available."
        assert return_interm_indices in [[0,1,2,3], [1,2,3], [3]]
        num_channels_all = [256, 512, 1024, 2048]
        num_channels = num_channels_all[4-len(return_interm_indices):]
        super().__init__(backbone, train_backbone, num_channels, return_interm_indices)


class Joiner(nn.Sequential):
    def __init__(self, backbone, position_embedding):
        super().__init__(backbone, position_embedding)

    def forward(self, tensor_list: NestedTensor):
        xs = self[0](tensor_list)
        out: List[NestedTensor] = []
        pos = []
        for name, x in xs.items():
            out.append(x)
            # position encoding
            pos.append(self[1](x).to(x.tensors.dtype))

        return out, pos


class VitBackboneAdapter(nn.Module):
    """
    Wraps your SSLVisionTransformer so it looks like a
    torchvision‐ResNet‐style backbone returning an OrderedDict
    of NestedTensor.
    """
    def __init__(self, vit: SSLVisionTransformer, out_indices, patch_size):
        super().__init__()
        self.vit = vit
        self.out_indices = out_indices
        self.patch_size = patch_size

    def forward(self, tensor_list: NestedTensor):
        x, mask = tensor_list.tensors, tensor_list.mask
        # 1) run ViT → returns tuple of B×C×H×W features
        feats = self.vit(x)

        out: OrderedDict[str, NestedTensor] = OrderedDict()
        for idx, feat in zip(self.out_indices, feats):
            # 2) downsample the mask to this feature’s resolution
            _, C, H, W = feat.shape
            m = F.interpolate(mask[None].float(),
                              size=(H, W),
                              mode="nearest")[0].to(torch.bool)
            out[f"stage{idx}"] = NestedTensor(feat, m)
        return out




def build_backbone(args):
    """
    Useful args:
        - backbone: backbone name
        - lr_backbone: 
        - dilation
        - return_interm_indices: available: [0,1,2,3], [1,2,3], [3]
        - backbone_freeze_keywords: 
        - use_checkpoint: for swin only for now

    """
    if args.backbone in ['resnet50', 'resnet101']:
        position_embedding = build_position_encoding(args)
        train_backbone = args.lr_backbone > 0
        if not train_backbone:
            raise ValueError("Please set lr_backbone > 0")
        return_interm_indices = args.return_interm_indices
        assert return_interm_indices in [[0,1,2,3], [1,2,3], [3]]
        backbone_freeze_keywords = args.backbone_freeze_keywords
        use_checkpoint = getattr(args, 'use_checkpoint', False)

        if args.backbone in ['resnet50', 'resnet101']:
            backbone = Backbone(args.backbone, train_backbone, args.dilation,   
                                    return_interm_indices,   
                                    batch_norm=FrozenBatchNorm2d)
            bb_num_channels = backbone.num_channels
        else:
            raise NotImplementedError("Unknown backbone {}".format(args.backbone))
        

        assert len(bb_num_channels) == len(return_interm_indices), f"len(bb_num_channels) {len(bb_num_channels)} != len(return_interm_indices) {len(return_interm_indices)}"

    elif args.backbone in ['SSLVisionTransformer', 'ssl_vit']:
        # instantiate your ViT
        vit = SSLVisionTransformer(
            img_size=args.img_size,
            patch_size=args.patch_size,
            embed_dim=args.embed_dim,
            depth=args.depth,
            num_heads=args.num_heads,
            drop_path_rate=args.drop_path_rate,
            mlp_ratio=args.mlp_ratio,
            qkv_bias=args.qkv_bias,
            pretrained=args.pretrained,
            out_indices=args.out_indices,
            frozen_stages=args.frozen_stages,
            init_cfg=dict(
                type='Pretrained', checkpoint='/Volumes/Macintosh HD/Users/work/Documents/GitHub/HighResCanopyHeight-main/saved_checkpoints/SSLhuge_satellite.pth')#/home/ubuntu/mmdetection/models/SSLhuge_satellite.pth')
        )
        # adapter → now it looks like a ResNet‐style backbone
        backbone = VitBackboneAdapter(
            vit=vit,
            out_indices=args.out_indices,
            patch_size=(args.patch_size, args.patch_size),
        )
        # these must match what your FPN produces before the Neck
        num_channels = [320, 640, args.embed_dim, args.embed_dim]

    else:
        raise NotImplementedError(f"Unknown backbone {args.backbone}")

    model = Joiner(backbone, position_embedding)
    model.num_channels = num_channels 
    assert isinstance(num_channels, List), "num_channels is expected to be a List but {}".format(type(num_channels))
    return model
