import logging
import time

import torch
import torch.nn as nn


class MultHeadAttentionLayer(nn.Module):
    def __init__(self, attention_norm, attn):
        super(MultHeadAttentionLayer, self).__init__()
        self.attention_norm = attention_norm
        self.attn = attn

    def forward(self, x):
        h = x
        x = self.attention_norm(x)
        x, weights = self.attn(x)
        x = x + h
        return x


class MLPLayer(nn.Module):
    def __init__(self, ffn_norm, ffn):
        super(MLPLayer, self).__init__()
        self.ffn_norm = ffn_norm
        self.ffn = ffn

    def forward(self, x):
        h = x
        x = self.ffn_norm(x)
        x = self.ffn(x)
        x = x + h
        return x


class OutputHead(nn.Module):
    def __init__(self, hidden_size, num_classes):
        super(OutputHead, self).__init__()
        self.head = nn.Linear(hidden_size, num_classes)

    def forward(self, x):
        logits = self.head(x[:, 0])
        return logits


"""
Issues Description:
the output of pipe is RRef，but DDP cannot recognize RRef object so DDP cannot find Tensor inside RRef.
Then DDP will view all parameters as used ones.

Temporal Solution:
Using a Wrapper model to help DDP find find Tensors inside RRef.
"""


class FrozenLayer(nn.Module):
    def __init__(self, num_frozen_layer, frozen_emb, frozen_layer_list):
        super().__init__()
        self.num_frozen_layer = num_frozen_layer
        self.embedding = frozen_emb
        self.layers = nn.ModuleList()
        for layer_i in range(num_frozen_layer):
            self.layers.append(frozen_layer_list[layer_i])

    def forward(self, x, layer_id=0):
        if layer_id == self.num_frozen_layer:
            logging.info("no need to recompute")
            return x
        if layer_id == 0:
            logging.info("compute from layer 0")
            x = self.embedding(x)
            for id in range(0, self.num_frozen_layer):
                x = self.layers[id](x)
            return x
        else:
            logging.info("compute from layer %d-%d" % (layer_id, self.num_frozen_layer-1))
            for id in range(layer_id, self.num_frozen_layer):
                x = self.layers[id](x)
            return x


class PipeModelWrapper(nn.Module):
    def __init__(self, pipe_model):
        super().__init__()
        self.pipe_model = pipe_model

    def forward(self, *args, **kwargs):
        return self.pipe_model(*args, **kwargs).local_value()


def count_parameters(model, b_is_required_grad=True):
    if b_is_required_grad:
        params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    else:
        params = sum(p.numel() for p in model.parameters())
    return params / 1000000


def create_pipe_styled_model(model_backbone, output_model, num_layer_in_total, num_frozen_layer):
    """
    Optimization:
        Pin Memory: https://pytorch.org/docs/stable/notes/cuda.html#use-pinned-memory-buffers
        Prepare a Pin Memory model
    """
    frozen_model = None
    pipe_model = nn.Sequential()

    parameters_size_frozen = 0.0
    parameters_list_pipe = []

    if num_frozen_layer > 0:
        for param in model_backbone.transformer.embeddings.parameters():
            param.requires_grad = False

        frozen_emb = model_backbone.transformer.embeddings

        size_embedding = count_parameters(model_backbone.transformer.embeddings, False)
        parameters_size_frozen += size_embedding

        frozen_layer_list = nn.ModuleList()
        for frozen_layer_index in range(num_frozen_layer):
            layer_block = model_backbone.transformer.encoder.layer[frozen_layer_index]
            for param in layer_block.parameters():
                param.requires_grad = False
            frozen_layer_list.append(layer_block)

            size_layer_block = count_parameters(layer_block, False)
            parameters_size_frozen += size_layer_block

        frozen_model = FrozenLayer(num_frozen_layer, frozen_emb, frozen_layer_list)
    else:
        pipe_model.add_module("embedding", model_backbone.transformer.embeddings)
        size_embedding = count_parameters(model_backbone.transformer.embeddings, False)
        parameters_list_pipe.append(size_embedding)

    # add transformer blocks needed to be trained
    for layer_index in range(num_frozen_layer, num_layer_in_total):
        layer_block = model_backbone.transformer.encoder.layer[layer_index]
        multihead_attention_layer = MultHeadAttentionLayer(layer_block.attention_norm, layer_block.attn)
        mlp_layer = MLPLayer(layer_block.ffn_norm, layer_block.ffn)
        pipe_model.add_module("multihead_attention_layer" + str(layer_index), multihead_attention_layer)
        pipe_model.add_module("mlp_layer" + str(layer_index), mlp_layer)

        size_multihead_attention_layer = count_parameters(multihead_attention_layer, False)
        parameters_list_pipe.append(size_multihead_attention_layer)

        size_mlp_layer = count_parameters(mlp_layer, False)
        parameters_list_pipe.append(size_mlp_layer)

    pipe_model.add_module("encoder_norm", model_backbone.transformer.encoder.encoder_norm)
    size_encoder_norm = count_parameters(model_backbone.transformer.encoder.encoder_norm, False)
    parameters_list_pipe.append(size_encoder_norm)

    pipe_model.add_module("head", output_model)
    size_output_model = count_parameters(output_model, False)
    parameters_list_pipe.append(size_output_model)

    # logging.info(frozen_model)
    # logging.info(parameters_size_frozen)
    # logging.info(pipe_model)
    # logging.info(parameters_list_pipe)

    return frozen_model, parameters_size_frozen, pipe_model, parameters_list_pipe


def convert_to_balanced_model(local_rank, global_rank,
                              device_idx_start, pipe: nn.Sequential, balance):
    # logging.info("device_idx_start = %d" % device_idx_start)
    # logging.info(pipe)
    # logging.info(balance)
    """
    Optimization:
        Pin Memory: https://pytorch.org/docs/stable/notes/cuda.html#use-pinned-memory-buffers
        Prepare a Pin Memory model
    """
    logging.info("convert_to_balanced_model. local_rank = %d, global_rank = %d" % (local_rank, global_rank))
    time_start_loading = time.time()
    pipe_layer_idx = 0
    balanced_pipe = []
    for device_id in balance.keys():
        num_layers = balance[device_id]
        layers = []
        for i in range(num_layers):
            layers.append(pipe[pipe_layer_idx])
            pipe_layer_idx += 1
        if torch.cuda.is_available():
            device = torch.device("cuda:" + str(device_id + device_idx_start))
            logging.info("######################local_rank = %d, global_rank = %d, device id: %d" % (local_rank,
                                                                                                     global_rank,
                                                                                                     device_id + device_idx_start))
            balanced_pipe.append(nn.Sequential(*layers).to(device, non_blocking=False))
        else:
            balanced_pipe.append(nn.Sequential(*layers))
    time_end_loading = time.time()
    logging.info("CPU->GPU time cost = " + str(time_end_loading - time_start_loading))
    return nn.Sequential(*balanced_pipe)


def freeze_layers_for_pipe_model(model, num_frozen_layers):
    ddp_ignore_name_list = []
    partition_idx = 0
    sub_layer_idx = 0
    for i in range(num_frozen_layers * 2 + 1):
        # the frozen layers may be split into multiple partitions
        if sub_layer_idx > len(model.partitions[partition_idx]) - 1:
            partition_idx += 1
            sub_layer_idx = 0

        for param in model.partitions[partition_idx][sub_layer_idx].parameters():
            param.requires_grad = False

        sub_layer_idx += 1

    logging.info(ddp_ignore_name_list)
    return ddp_ignore_name_list


def freeze_layers_for_normal_model(model, num_frozen_layers):
    if num_frozen_layers > 0:
        for param in model.transformer.embeddings.parameters():
            param.requires_grad = False
        for frozen_layer_index in range(num_frozen_layers):
            layer_block = model.transformer.encoder.layer[frozen_layer_index]
            for param in layer_block.parameters():
                param.requires_grad = False


def get_ddp_ignored_params_name(model, num_frozen_layers):
    if num_frozen_layers == 0:
        return []

    def get_name_list_to_ignore_comm_in_ddp(model, model_module):
        model_emb_name = [
            module_name
            for module_name, module in model.named_modules()
            if module is model_module
        ][0]
        proxy_param_names = [
            f"{model_emb_name}.{param_name}"
            for param_name, _ in model_module.named_parameters()
        ]
        proxy_buffer_names = [
            f"{model_emb_name}.{buf_name}"
            for buf_name, _ in model_module.named_buffers()
        ]
        return proxy_param_names + proxy_buffer_names

    ddp_ignore_name_list = []
    partition_idx = 0
    sub_layer_idx = 0
    for i in range(num_frozen_layers * 2 + 1):
        # the frozen layers may be split into multiple partitions
        if sub_layer_idx > len(model.partitions[partition_idx]) - 1:
            partition_idx += 1
            sub_layer_idx = 0

        name_list = get_name_list_to_ignore_comm_in_ddp(model, model.partitions[partition_idx][sub_layer_idx])
        # logging.info(name_list)
        ddp_ignore_name_list += name_list

        sub_layer_idx += 1

    logging.info(ddp_ignore_name_list)
    return ddp_ignore_name_list