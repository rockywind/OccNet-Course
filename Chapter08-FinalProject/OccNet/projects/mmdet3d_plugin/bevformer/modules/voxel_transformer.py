# ---------------------------------------------
# Copyright (c) OpenMMLab. All rights reserved.
# ---------------------------------------------

import numpy as np
import torch
import torch.nn as nn
from mmcv.cnn import xavier_init
from mmcv.cnn.bricks.transformer import build_transformer_layer_sequence
from mmcv.runner import auto_fp16
from mmcv.runner.base_module import BaseModule
from mmdet.models.utils.builder import TRANSFORMER
from torch.nn.init import normal_
from torchvision.transforms.functional import rotate

from .decoder import CustomMSDeformableAttention
from .spatial_cross_attention import MSDeformableAttention3D
from .temporal_self_attention import TemporalSelfAttention


@TRANSFORMER.register_module()
class VoxelPerceptionTransformer(BaseModule):
    """Implements the Detr3D transformer.
    Args:
        as_two_stage (bool): Generate query from encoder features.
            Default: False.
        num_feature_levels (int): Number of feature maps from FPN:
            Default: 4.
        two_stage_num_proposals (int): Number of proposals when set
            `as_two_stage` as True. Default: 300.
    """

    def __init__(
        self,
        num_feature_levels=4,
        num_cams=6,
        two_stage_num_proposals=300,
        encoder=None,
        decoder=None,
        embed_dims=256,
        rotate_prev_bev=True,
        use_shift=True,
        use_can_bus=True,
        can_bus_norm=True,
        can_bus_in_dataset=True,
        use_cams_embeds=True,
        rotate_center=[100, 100],
        decoder_on_bev=False,
        voxel_2_bev_type="mlp",
        bev_z=1,
        **kwargs
    ):
        super(VoxelPerceptionTransformer, self).__init__(**kwargs)
        self.encoder = build_transformer_layer_sequence(encoder)
        if decoder is not None:
            self.decoder = build_transformer_layer_sequence(decoder)
        else:
            self.decoder = None
        self.embed_dims = embed_dims
        self.num_feature_levels = num_feature_levels
        self.num_cams = num_cams
        self.fp16_enabled = False

        self.rotate_prev_bev = rotate_prev_bev
        self.use_shift = use_shift
        self.use_can_bus = use_can_bus
        self.can_bus_norm = can_bus_norm
        self.can_bus_in_dataset = can_bus_in_dataset
        self.use_cams_embeds = use_cams_embeds
        self.decoder_on_bev = decoder_on_bev
        self.voxel_2_bev_type = voxel_2_bev_type
        self.bev_z = bev_z
        self.two_stage_num_proposals = two_stage_num_proposals

        self.init_layers()
        self.rotate_center = rotate_center

    def init_layers(self):
        """Initialize layers of the Detr3DTransformer."""
        self.level_embeds = nn.Parameter(
            torch.Tensor(self.num_feature_levels, self.embed_dims)
        )
        if self.use_cams_embeds:
            self.cams_embeds = nn.Parameter(
                torch.Tensor(self.num_cams, self.embed_dims)
            )
        if self.use_can_bus:
            self.can_bus_mlp = nn.Sequential(
                nn.Linear(18, self.embed_dims // 2),
                nn.ReLU(inplace=True),
                nn.Linear(self.embed_dims // 2, self.embed_dims),
                nn.ReLU(inplace=True),
            )
            if self.can_bus_norm:
                self.can_bus_mlp.add_module("norm", nn.LayerNorm(self.embed_dims))
        if self.decoder is not None:
            self.reference_points = nn.Linear(self.embed_dims, 3)
        if (
            self.decoder is not None
            and self.decoder_on_bev
            and self.voxel_2_bev_type == "mlp"
        ):
            # self.voxel2bev = nn.Linear(self.embed_dims*self.bev_z, self.embed_dims)
            voxel2bev = []
            mid_num = self.embed_dims * self.bev_z
            voxel2bev.append(nn.Linear(self.embed_dims * self.bev_z, mid_num))
            voxel2bev.append(nn.LayerNorm(mid_num))
            voxel2bev.append(nn.ReLU(inplace=True))
            voxel2bev.append(nn.Linear(mid_num, self.embed_dims))
            voxel2bev.append(nn.LayerNorm(self.embed_dims))
            voxel2bev.append(nn.ReLU(inplace=True))
            self.voxel2bev = nn.Sequential(*voxel2bev)

    def init_weights(self):
        """Initialize the transformer weights."""
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)
        for m in self.modules():
            if (
                isinstance(m, MSDeformableAttention3D)
                or isinstance(m, TemporalSelfAttention)
                or isinstance(m, CustomMSDeformableAttention)
            ):
                try:
                    m.init_weight()
                except AttributeError:
                    m.init_weights()
        normal_(self.level_embeds)
        if self.use_cams_embeds:
            normal_(self.cams_embeds)
        if self.use_can_bus:
            xavier_init(self.can_bus_mlp, distribution="uniform", bias=0.0)
        if self.decoder is not None:
            xavier_init(self.reference_points, distribution="uniform", bias=0.0)
        if (
            self.decoder is not None
            and self.decoder_on_bev
            and self.voxel_2_bev_type == "mlp"
        ):
            xavier_init(self.voxel2bev, distribution="uniform", bias=0.0)

    @auto_fp16(apply_to=("mlvl_feats", "bev_queries", "prev_bev", "bev_pos"))
    def get_voxel_features(
        self,
        mlvl_feats,
        bev_queries,
        bev_z,
        bev_h,
        bev_w,
        grid_length=[0.512, 0.512],
        bev_pos=None,
        prev_bev=None,
        **kwargs
    ):
        """
        obtain bev features.
        """

        bs = mlvl_feats[0].size(0)
        bev_queries = bev_queries.unsqueeze(1).repeat(
            1, bs, 1
        )  # (num_query, bs, embed_dims)
        bev_pos = bev_pos.flatten(2).permute(2, 0, 1)  # (num_query, bs, embed_dims)

        # obtain rotation angle and shift with ego motion
        if self.can_bus_in_dataset:
            delta_x = np.array([each["can_bus"][0] for each in kwargs["img_metas"]])
            delta_y = np.array([each["can_bus"][1] for each in kwargs["img_metas"]])
            ego_angle = np.array(
                [each["can_bus"][-2] / np.pi * 180 for each in kwargs["img_metas"]]
            )
            grid_length_y = grid_length[0]
            grid_length_x = grid_length[1]
            translation_length = np.sqrt(delta_x ** 2 + delta_y ** 2)
            translation_angle = np.arctan2(delta_y, delta_x) / np.pi * 180
            bev_angle = ego_angle - translation_angle
            shift_y = (
                translation_length
                * np.cos(bev_angle / 180 * np.pi)
                / grid_length_y
                / bev_h
            )
            shift_x = (
                translation_length
                * np.sin(bev_angle / 180 * np.pi)
                / grid_length_x
                / bev_w
            )
        else:
            shift_y = np.array([0] * bs)
            shift_x = np.array([0] * bs)
        shift_y = shift_y * self.use_shift
        shift_x = shift_x * self.use_shift
        shift = bev_queries.new_tensor([shift_x, shift_y]).permute(
            1, 0
        )  # (2, bs) -> (bs, 2)
        if prev_bev is not None:  # (bs, num_query, embed_dims)
            if prev_bev.shape[1] == bev_z * bev_h * bev_w:
                prev_bev = prev_bev.permute(1, 0, 2)  # (num_query, bs, embed_dims)
            if self.rotate_prev_bev:  # revise for 3D feature map
                for i in range(bs):
                    rotation_angle = kwargs["img_metas"][i]["can_bus"][-1]
                    tmp_prev_bev = (
                        prev_bev[:, i]
                        .reshape(bev_z, bev_h, bev_w, -1)
                        .permute(3, 0, 1, 2)
                    )  # (embed_dims, bev_z, bev_h, bev_w)
                    tmp_prev_bev = rotate(
                        tmp_prev_bev, rotation_angle, center=self.rotate_center
                    )
                    tmp_prev_bev = tmp_prev_bev.permute(1, 2, 3, 0).reshape(
                        bev_z * bev_h * bev_w, 1, -1
                    )
                    prev_bev[:, i] = tmp_prev_bev[:, 0]

        # add can bus signals
        if self.use_can_bus:
            can_bus = bev_queries.new_tensor(
                [each["can_bus"] for each in kwargs["img_metas"]]
            )  # [:, :]
            can_bus = self.can_bus_mlp(can_bus)[None, :, :]
            bev_queries = (
                bev_queries + can_bus * self.use_can_bus
            )  # (query_num, bs, embed_dims)

        feat_flatten = []
        spatial_shapes = []
        for lvl, feat in enumerate(mlvl_feats):
            bs, num_cam, c, h, w = feat.shape
            spatial_shape = (h, w)
            feat = feat.flatten(3).permute(1, 0, 3, 2)
            if self.use_cams_embeds:
                feat = feat + self.cams_embeds[:, None, None, :].to(feat.dtype)
            feat = feat + self.level_embeds[
                None, None, lvl : lvl + 1, :  # noqa: E203
            ].to(  # noqa: E203
                feat.dtype
            )  # noqa: E203
            spatial_shapes.append(spatial_shape)
            feat_flatten.append(feat)

        feat_flatten = torch.cat(feat_flatten, 2)
        spatial_shapes = torch.as_tensor(
            spatial_shapes, dtype=torch.long, device=bev_pos.device
        )
        level_start_index = torch.cat(
            (spatial_shapes.new_zeros((1,)), spatial_shapes.prod(1).cumsum(0)[:-1])
        )

        feat_flatten = feat_flatten.permute(
            0, 2, 1, 3
        )  # (num_cam, H*W, bs, embed_dims)

        bev_embed = self.encoder(
            bev_queries,
            feat_flatten,
            feat_flatten,
            bev_z=bev_z,
            bev_h=bev_h,
            bev_w=bev_w,
            bev_pos=bev_pos,
            spatial_shapes=spatial_shapes,
            level_start_index=level_start_index,
            prev_bev=prev_bev,
            shift=shift,
            **kwargs
        )

        return bev_embed

    @auto_fp16(
        apply_to=(
            "mlvl_feats",
            "bev_queries",
            "object_query_embed",
            "prev_bev",
            "bev_pos",
        )
    )
    def forward(
        self,
        mlvl_feats,
        bev_queries,
        object_query_embed,
        bev_z,
        bev_h,
        bev_w,
        grid_length=[0.512, 0.512],
        bev_pos=None,
        reg_branches=None,
        cls_branches=None,
        prev_bev=None,
        **kwargs
    ):
        """Forward function for `Detr3DTransformer`.
        Args:
            mlvl_feats (list(Tensor)): Input queries from
                different level. Each element has shape
                [bs, num_cams, embed_dims, h, w].
            bev_queries (Tensor): (bev_h*bev_w, c)
            bev_pos (Tensor): (bs, embed_dims, bev_h, bev_w)
            object_query_embed (Tensor): The query embedding for decoder,
                with shape [num_query, c].
            reg_branches (obj:`nn.ModuleList`): Regression heads for
                feature maps from each decoder layer. Only would
                be passed when `with_box_refine` is True. Default to None.
        Returns:
            tuple[Tensor]: results of decoder containing the following tensor.
                - bev_embed: BEV features
                - inter_states: Outputs from decoder. If
                    return_intermediate_dec is True output has shape \
                      (num_dec_layers, bs, num_query, embed_dims), else has \
                      shape (1, bs, num_query, embed_dims).
                - init_reference_out: The initial value of reference \
                    points, has shape (bs, num_queries, 4).
                - inter_references_out: The internal value of reference \
                    points in decoder, has shape \
                    (num_dec_layers, bs,num_query, embed_dims)
                - enc_outputs_class: The classification score of \
                    proposals generated from \
                    encoder's feature maps, has shape \
                    (batch, h*w, num_classes). \
                    Only would be returned when `as_two_stage` is True, \
                    otherwise None.
                - enc_outputs_coord_unact: The regression results \
                    generated from encoder's feature maps., has shape \
                    (batch, h*w, 4). Only would \
                    be returned when `as_two_stage` is True, \
                    otherwise None.
        """

        voxel_embed = self.get_voxel_features(
            mlvl_feats,
            bev_queries,
            bev_z,
            bev_h,
            bev_w,
            grid_length=grid_length,
            bev_pos=bev_pos,
            prev_bev=prev_bev,
            **kwargs
        )  # voxel_embed shape: (bs, num_query, embed_dims)

        bs = mlvl_feats[0].size(0)
        # object_query_embed  (num_box_query, embed_dims*2)
        query_pos, query = torch.split(object_query_embed, self.embed_dims, dim=1)
        query_pos = query_pos.unsqueeze(0).expand(
            bs, -1, -1
        )  # (bs, num_box_query, embed_dims)
        query = query.unsqueeze(0).expand(bs, -1, -1)  # (bs, num_box_query, embed_dims)
        reference_points = self.reference_points(query_pos)  # (bs, num_box_query, 3)
        reference_points = reference_points.sigmoid()
        init_reference_out = reference_points

        query = query.permute(1, 0, 2)
        query_pos = query_pos.permute(1, 0, 2)
        voxel_embed = voxel_embed.permute(1, 0, 2)  # (num_query, bs, embed_dims)

        if self.decoder_on_bev:
            if self.voxel_2_bev_type == "mlp":
                bev_embed = voxel_embed.view(bev_z, bev_h, bev_w, bs, self.embed_dims)
                bev_embed = bev_embed.permute(1, 2, 3, 0, 4)
                bev_embed = bev_embed.flatten(3)
                bev_embed = self.voxel2bev(bev_embed)
            elif self.voxel_2_bev_type == "pool":
                bev_embed = voxel_embed.view(bev_z, bev_h, bev_w, bs, self.embed_dims)
                bev_embed = bev_embed.permute(1, 2, 3, 4, 0)
                bev_embed = torch.max(bev_embed, dim=-1)[
                    0
                ]  # (p1 + p2 + ... + pb, out_channels)
            bev_embed = bev_embed.view(-1, bs, self.embed_dims)

            inter_states, inter_references = self.decoder(
                query=query,
                key=None,
                value=bev_embed,
                query_pos=query_pos,
                reference_points=reference_points,
                reg_branches=reg_branches,
                cls_branches=cls_branches,
                spatial_shapes=torch.tensor([[bev_h, bev_w]], device=query.device),
                level_start_index=torch.tensor([0], device=query.device),
                **kwargs
            )
        else:
            inter_states, inter_references = self.decoder(
                query=query,
                key=None,
                value=voxel_embed,
                query_pos=query_pos,
                reference_points=reference_points,
                reg_branches=reg_branches,
                cls_branches=cls_branches,
                spatial_shapes=torch.tensor(
                    [[bev_z, bev_h, bev_w]], device=query.device
                ),
                level_start_index=torch.tensor([0], device=query.device),
                **kwargs
            )

        inter_references_out = inter_references

        return voxel_embed, inter_states, init_reference_out, inter_references_out
