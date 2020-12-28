import gc
import logging
import os

import torch
import torch.nn
import torch.distributed as dist
from torch import nn
from torch.distributed import rpc, timedelta, Backend
from torch.distributed.rpc import TensorPipeRpcBackendOptions
from torch.nn.parallel import DistributedDataParallel as DDP

from dp.distributed_communicator import dist_broadcast


class AutoDataParallel:

    def __init__(self, initial_pipe_len):
        self.local_rank = -1
        self.global_rank = -1
        self.world_size = -1
        self.master_addr = "127.0.0.1"
        self.global_port = 11111
        self.active_process_group = None

        self.initial_pipe_len = initial_pipe_len
        self.compressed_pipe_len = initial_pipe_len

        self.first_run = True

        self.active_ranks = []
        self.newly_added_active_ranks = []
        # key: rank; value: data_rank
        self.active_data_ranks = dict()
        self.freeze_point = None

    def get_ddp_model(self, model, local_rank):
        return DDP(model, device_ids=[local_rank], output_device=local_rank)

    def init_ddp(self, args):
        self.local_rank = args.local_rank
        print(f"Running basic DDP example on local rank {self.local_rank}.")

        self.global_port += 1
        if args.is_infiniband:
            # self.master_addr = "192.168.11.1"
            self.master_addr = "192.168.11.2"
        else:
            # self.master_addr = "192.168.1.1"
            self.master_addr = "192.168.11.2"
        os.environ.update({"MASTER_ADDR": self.master_addr})
        os.environ.update({"MASTER_PORT": str(self.global_port)})

        # use InfiniBand
        # os.environ['NCCL_DEBUG'] = 'INFO'

        if args.is_infiniband:
            os.environ['NCCL_SOCKET_IFNAME'] = 'ib0'
            os.environ['GLOO_SOCKET_IFNAME'] = 'ib0'
            os.environ['TP_SOCKET_IFNAME'] = 'ib0'
        else:
            os.environ['NCCL_IB_DISABLE'] = '1'
            os.environ['NCCL_TREE_THRESHOLD'] = '0'
            os.environ['NCCL_SOCKET_IFNAME'] = 'eno2'
            os.environ['GLOO_SOCKET_IFNAME'] = 'eno2'
            os.environ['TP_SOCKET_IFNAME'] = 'eno2'

        # This the global rank: 0, 1, 2, ..., 15
        self.global_rank = int(os.environ['RANK'])
        print("int(os.environ['RANK']) = %d" % self.global_rank)

        # This the globak world_size
        self.world_size = int(os.environ['WORLD_SIZE'])
        print("world_size = %d" % self.world_size)

        # initialize the process group
        dist.init_process_group(init_method='tcp://' + self.master_addr + ':' + str(self.global_port),
                                backend=Backend.NCCL, rank=self.global_rank, world_size=self.world_size)
        print("init_process_group. local_rank = %d, global_rank = %d" % (self.local_rank, self.global_rank))

    def get_local_rank(self):
        return self.local_rank

    def get_global_rank(self):
        return self.global_rank

    def get_world_size(self):
        return self.world_size

    def get_active_world_size(self):
        return len(self.active_ranks)

    def init_rpc(self):
        rpc_backend_options = TensorPipeRpcBackendOptions()
        rpc_backend_options.init_method = 'tcp://' + self.master_addr + ':10000'
        rpc.init_rpc(
            "worker:" + str(self.global_rank),
            rank=self.global_rank,
            world_size=self.world_size,
            rpc_backend_options=rpc_backend_options,
        )
        print("init_rpc")

    def warm_up(self):
        class WarmupModel(torch.nn.Module):
            def __init__(self):
                super(WarmupModel, self).__init__()
                self.model_arch = torch.nn.Parameter(1e-3*torch.randn(1,1))

            def forward(self, x):
                x = self.model_arch*2
                return x
        warmup_model = WarmupModel()
        warmup_model.to(self.local_rank)
        print("local_rank = %d, global_rank = %d" % (self.local_rank, self.global_rank))

    def is_active(self):
        return True if self.global_rank in self.active_ranks else False

    def get_active_ranks(self):
        return self.active_ranks

    def update_active_ranks(self):
        # update active ranks
        new_active_ranks = []
        data_rank = 0
        pipe_num = int(self.initial_pipe_len / self.compressed_pipe_len)
        if self.world_size < self.initial_pipe_len:
            raise Exception("world_size should be divided by self.initial_pipe_len")
        else:
            ddp_num = int(self.world_size / self.initial_pipe_len)
        for dp_idx in range(ddp_num):
            start_rank = dp_idx * self.initial_pipe_len
            for active_rank in range(pipe_num):
                active_rank += start_rank
                new_active_ranks.append(active_rank)
                self.active_data_ranks[active_rank] = data_rank
                data_rank += 1
        print("active ranks = " + str(self.active_ranks))
        self.newly_added_active_ranks = self._diff_list(new_active_ranks, self.active_ranks)
        self.active_ranks.clear()
        self.active_ranks = new_active_ranks

    def get_newly_added_active_ranks(self):
        return self.newly_added_active_ranks

    def create_active_process_group(self):
        if self.active_process_group is None:
            del self.active_process_group
        self.update_active_ranks()
        print("get_active_process_group - auto_pipe.get_active_ranks() = " + str(self.active_ranks))
        print("local_rank = %d, global_rank = %d - *************************create_active_process_group*********"
              % (self.local_rank, self.global_rank))
        self.active_process_group = dist.new_group(ranks=self.active_ranks, backend=Backend.NCCL,
                                                   timeout=timedelta(days=365))

    def generate_ddp_model(self, model, gpu_num_per_process, ddp_params_to_skip):
        """
        Issues Description:
        the output of pipe is RRef，but DDP cannot recognize RRef object so DDP cannot find Tensor inside RRef.
        Then DDP will view all parameters as used ones.

        Temporal Solution:
        Using a Wrapper model to help DDP find find Tensors inside RRef.
        """
        class Wrapper(nn.Module):
            def __init__(self, pipe_module):
                super().__init__()
                self.pipe_module = pipe_module

            def forward(self, *args, **kwargs):
                return self.pipe_module(*args, **kwargs).local_value()

        self.pipe_len = gpu_num_per_process
        DDP._set_params_and_buffers_to_ignore_for_model(model, ddp_params_to_skip)
        if gpu_num_per_process > 1:
            # find_unused_parameters = True can avoid bucket rebuilt, which takes around 20s
            model = DDP(Wrapper(model), process_group=self.active_process_group,
                        find_unused_parameters=True)
            # model = DDP(Wrapper(model), process_group=self.active_process_group)
        else:
            # find_unused_parameters = True can avoid bucket rebuilt, which takes around 20s
            model = DDP(Wrapper(model), device_ids=[self.local_rank], process_group=self.active_process_group,
                        find_unused_parameters=True)
            # model = DDP(Wrapper(model), device_ids=[self.local_rank], process_group=self.active_process_group,
            #             find_unused_parameters=True)
        return model

    def get_freeze_point(self):
        return self.freeze_point

    def get_data_rank(self):
        self.update_active_ranks()
        return self.active_data_ranks[self.global_rank]

    def transform(self, auto_pipe, model, num_frozen_layers, freeze_point):
        self.freeze_point = freeze_point
        if auto_pipe.get_num_frozen_layers() == num_frozen_layers:
            return model
        if num_frozen_layers == 0 and auto_pipe.get_pipe_len() == 1:
            return model

        # create the initial group only once
        if self.first_run:
            self.create_active_process_group()
            self.clear_memory()
            self.first_run = False

        if self.is_active():
            model = self._active_process_impl(auto_pipe, num_frozen_layers)
        else:
            model = self._inactive_process_impl(auto_pipe)
        return model

    def _active_process_impl(self, auto_pipe, num_frozen_layers):
        model, pipe_len, ddp_params_to_skip = auto_pipe.transform(num_frozen_layers)
        if self.compressed_pipe_len != pipe_len:
            self.compressed_pipe_len = pipe_len
            self.update_active_ranks()

            # broadcast control messages
            print("####### broad cast control message (num_frozen_layers, pipe_len) to all processes #######")
            max_parameter_per_gpu_at_beginning = auto_pipe.get_max_parameter_per_gpu_at_beginning()
            broad_cast_msg = self._build_broad_cast_message(num_frozen_layers, pipe_len,
                                                            max_parameter_per_gpu_at_beginning, self.freeze_point)
            if self.global_rank == 0:
                print("local_rank = %d, global_rank = %d - *************************dist_send send(START) "
                      % (self.local_rank, self.global_rank))
                dist_broadcast(0, broad_cast_msg)
                print("local_rank = %d, global_rank = %d - *************************dist_send send(END)"
                      % (self.local_rank, self.global_rank))
            else:
                dist_broadcast(0, broad_cast_msg)

            self.create_active_process_group()
            self.clear_memory()
        model = self.generate_ddp_model(model, pipe_len, ddp_params_to_skip)
        return model

    def _inactive_process_impl(self, auto_pipe):
        broad_cast_msg = [None, None, None, None, None]
        frozen_message = dist_broadcast(0, broad_cast_msg)
        num_frozen_layers, pipe_len, max_parameter_per_gpu_at_beginning, \
        newly_added_active_ranks, freeze_point = self._parse_broad_cast_message(frozen_message)

        self.compressed_pipe_len = pipe_len
        auto_pipe.set_pipe_len(pipe_len)
        auto_pipe.set_max_parameter_per_gpu_at_beginning(max_parameter_per_gpu_at_beginning)
        self.create_active_process_group()
        self.clear_memory()

        if self.global_rank in newly_added_active_ranks:
            print("global_rank %d is activated!" % self.global_rank)

            model, pipe_len, ddp_params_to_skip = auto_pipe.transform(num_frozen_layers)
            model = self.generate_ddp_model(model, pipe_len, ddp_params_to_skip)
        else:
            model = self._inactive_process_impl(auto_pipe)
        return model

    def _build_broad_cast_message(self, num_frozen_layers, pipe_len, max_parameter_per_gpu_at_beginning, freeze_point):
        print("self.newly_added_active_ranks = " + str(self.newly_added_active_ranks))
        broad_cast_msg = [num_frozen_layers, pipe_len, max_parameter_per_gpu_at_beginning,
                          freeze_point['epoch'], self.newly_added_active_ranks]
        return broad_cast_msg

    def _parse_broad_cast_message(self, frozen_message):
        print("local_rank = %d, global_rank = %d - frozen_message = %s" % (
            self.local_rank, self.global_rank, frozen_message))

        num_frozen_layers = int(frozen_message[0])
        pipe_len = int(frozen_message[1])
        max_parameter_per_gpu_at_beginning = frozen_message[2]
        print("local_rank = %d, global_rank = %d - frozen_layer_num = %s" % (
            self.local_rank, self.global_rank, num_frozen_layers))
        print("local_rank = %d, global_rank = %d - pipe_len = %s" % (self.local_rank, self.global_rank, pipe_len))
        epoch_start = int(frozen_message[3])
        freeze_point = dict()
        freeze_point['epoch'] = epoch_start
        self.freeze_point = freeze_point
        newly_added_active_ranks = frozen_message[5]
        print("newly_added_active_ranks = " + str(newly_added_active_ranks))
        return num_frozen_layers, pipe_len, max_parameter_per_gpu_at_beginning, newly_added_active_ranks, freeze_point

    def observe_params_communicated(self, model):
        def my_hook(state, bucket):
            params_size = sum([t.numel() for t in bucket.get_tensors()])
            global size_params_ddp_sum
            size_params_ddp_sum += params_size
            logging.info(params_size / 1000 / 1000)
            fut = torch.futures.Future()
            fut.set_result(bucket.get_tensors())
            return fut

        model.register_comm_hook(state=None, hook=my_hook)

    def clear_memory(self):
        # dist.destroy_process_group()
        torch.cuda.empty_cache()
        gc.collect()

    def _diff_list(self, li1, li2):
        return (list(list(set(li1) - set(li2)) + list(set(li2) - set(li1))))
