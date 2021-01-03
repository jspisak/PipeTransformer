import logging
import math
import os
import os.path
import pickle
import shutil
import time
from os import path

import psutil
import torch
import torch.multiprocessing as mp
import wandb
from torch.nn import Linear

"""
When disk is OOM:

Traceback (most recent call last):
  File "/home/chaoyanghe/anaconda3/envs/pipe_ditributed/lib/python3.7/multiprocessing/queues.py", line 236, in _feed
  File "/home/chaoyanghe/anaconda3/envs/pipe_ditributed/lib/python3.7/multiprocessing/reduction.py", line 51, in dumps
    cls(buf, protocol).dump(obj)
  File "/home/chaoyanghe/anaconda3/envs/pipe_ditributed/lib/python3.7/site-packages/torch/multiprocessing/reductions.py", line 321, in reduce_storage
RuntimeError: unable to open shared memory object </torch_52868_1950128645> in read-write mode

When a chunk is too big to hold by a pickle file:

Traceback (most recent call last):
  File "/home/chaoyanghe/anaconda3/envs/pipe_ditributed/lib/python3.7/multiprocessing/process.py", line 297, in _bootstrap
    self.run()
  File "/home/chaoyanghe/anaconda3/envs/pipe_ditributed/lib/python3.7/multiprocessing/process.py", line 99, in run
    self._target(*self._args, **self._kwargs)
  File "test.py", line 28, in disk_cache_process_impl
    save_as_pickle_file(path, chunk_idx, data_queue_list, chunk_index_list_to_be_cached)
  File "test.py", line 91, in save_as_pickle_file
    pickle.dump(numpy_queue, open(path + str(chunk_idx) + ".fea", "wb"))
OverflowError: cannot serialize a bytes object larger than 4 GiB
"""


def disk_cache_process_impl(cache_path, data_queue_list, msg_q):
    host_memory_window_len = -1

    chunk_idx_starting_disk_cache = -1
    chunk_idx_starting_recompute = -1
    chunk_num = -1
    while True:
        (msg_id, msg_params) = msg_q.get()
        if chunk_num == -1:
            chunk_num = msg_params['chunk_num']
        if msg_id == TwoLevelCache.MSG_TYPE_WRITING:
            logging.info("AutoCache.MSG_TYPE_WRITING")
            chunk_idx = msg_params['chunk_idx']
            if chunk_idx_starting_disk_cache != -1 and chunk_idx_starting_recompute == -1:
                chunk_index_list_to_be_cached = []
                for idx in range(chunk_idx_starting_disk_cache, chunk_idx):
                    chunk_index_list_to_be_cached.append(idx)
                logging.info("chunk_idx_starting_recompute = %d, chunk_index_list_to_be_cached = %s" % (
                    chunk_idx_starting_recompute, str(chunk_index_list_to_be_cached)))
                # cache to disk with pickle file
                save_as_pickle_file(cache_path, chunk_idx, data_queue_list, chunk_index_list_to_be_cached)
            elif chunk_idx_starting_recompute != -1:
                logging.info("disk memory is full, do nothing")
            else:
                logging.info("using host memory, do nothing")

        elif msg_id == TwoLevelCache.MSG_TYPE_WRITING_HOST_MEMORY_FULL:
            if chunk_idx_starting_disk_cache == -1:
                logging.info("AutoCache.MSG_TYPE_WRITING_HOST_MEMORY_FULL")
                chunk_idx_starting_disk_cache = msg_params['chunk_idx']
                host_memory_window_len = chunk_idx_starting_disk_cache

        elif msg_id == TwoLevelCache.MSG_TYPE_WRITING_DISK_MEMORY_FULL:
            if chunk_idx_starting_recompute == -1:
                chunk_idx_starting_recompute = msg_params['chunk_idx']
                logging.info(
                    "AutoCache.MSG_TYPE_WRITING_DISK_MEMORY_FULL. chunk_idx_starting_recompute = %d" % chunk_idx_starting_recompute)
            if chunk_idx_starting_disk_cache == -1:
                chunk_idx_starting_disk_cache = msg_params['chunk_idx']
                host_memory_window_len = chunk_idx_starting_disk_cache
        elif msg_id == TwoLevelCache.MSG_TYPE_READING:
            logging.info("AutoCache.MSG_TYPE_READING")
            # 1. find the chunk index list that should be loaded into the host memory,
            # and the chunk list that should be put into the disk storage
            chunk_idx = msg_params['chunk_idx']
            chunk_index_list_to_in_disk, chunk_index_list_to_in_memory = find_the_chunks_for_load_and_cache(
                host_memory_window_len,
                chunk_idx,
                chunk_idx_starting_recompute,
                chunk_num)
            logging.info(
                "chunk_idx = %d, chunk_index_list_to_in_memory = %s" % (chunk_idx, str(chunk_index_list_to_in_memory)))
            logging.info(
                "chunk_idx = %d, chunk_index_list_to_in_disk = %s" % (chunk_idx, str(chunk_index_list_to_in_disk)))

            # 2. load from disk storage
            load_from_pickle_file(cache_path, data_queue_list, chunk_index_list_to_in_memory)

            # 3. cache to disk with pickle file
            save_as_pickle_file(cache_path, chunk_idx, data_queue_list, chunk_index_list_to_in_disk)
        elif msg_id == TwoLevelCache.MSG_TYPE_TERMINATE:
            break
        else:
            raise Exception("no such message")
        logging.info("subprocess is running")


def find_the_chunks_for_load_and_cache(window_len, chunk_idx, chunk_idx_starting_recompute, chunk_num):
    """
        A sliding window algorithm to find the load and cache.
        THe high principle is always to keep window_len chunks in host memory.
    """
    logging.info("0 - chunk_idx = %d, window_len = %d, chunk_idx_starting_recompute = %d, chunk_num = %d" % (
        chunk_idx, window_len,
        chunk_idx_starting_recompute, chunk_num))
    if chunk_idx > chunk_idx_starting_recompute:
        return [], []
    chunk_num_in_cache = chunk_idx_starting_recompute
    chunk_index_list_to_in_disk = []
    chunk_index_list_to_in_memory = []
    if chunk_idx + window_len - 1 <= chunk_num_in_cache - 1:
        logging.info("1 - chunk_idx = %d, window_len = %d, chunk_idx_starting_recompute = %d, chunk_num = %d" % (
            chunk_idx, window_len,
            chunk_idx_starting_recompute, chunk_num))
        for idx in range(chunk_idx, chunk_idx + window_len):
            chunk_index_list_to_in_memory.append(idx)
        for idx in range(chunk_idx):
            chunk_index_list_to_in_disk.append(idx)
        for idx in range(chunk_idx + window_len, chunk_num_in_cache):
            chunk_index_list_to_in_disk.append(idx)

    elif chunk_idx + window_len - 1 > chunk_num_in_cache - 1:
        logging.info("2 - chunk_idx = %d, window_len = %d, chunk_idx_starting_recompute = %d, chunk_num = %d" % (
            chunk_idx, window_len,
            chunk_idx_starting_recompute, chunk_num))
        disk_start_chunk = chunk_idx + window_len - 1 - (chunk_num_in_cache - 1)
        disk_end_chunk = chunk_idx
        for idx in range(disk_start_chunk):
            chunk_index_list_to_in_memory.append(idx)
        for idx in range(chunk_idx, chunk_num_in_cache):
            chunk_index_list_to_in_memory.append(idx)
        for idx in range(disk_start_chunk, disk_end_chunk):
            chunk_index_list_to_in_disk.append(idx)
    return chunk_index_list_to_in_disk, chunk_index_list_to_in_memory


def move_tensor_queue_to_numpy_dict(tensor_queue):
    numpy_dict = dict()
    idx = 0
    while not tensor_queue.empty():
        tensor = tensor_queue.get()
        numpy_dict[idx] = tensor.numpy()
        idx += 1
    return numpy_dict


def move_numpy_dict_to_tensor_queue(data_queue_list, chunk_idx, numpy_dict):
    for idx in range(len(numpy_dict.keys())):
        numpy_feature = numpy_dict[idx]
        # print("numpy_feature = " + str(numpy_feature))
        tensor = torch.from_numpy(numpy_feature)
        data_queue_list[chunk_idx].put(tensor)


def save_as_pickle_file(path, chunk_idx, data_queue_list, chunk_index_list_to_be_cached):
    logging.info("chunk_idx = %d, chunk_index_list_to_be_cached = %s" % (chunk_idx, str(chunk_index_list_to_be_cached)))
    for chunk_idx in chunk_index_list_to_be_cached:
        if not data_queue_list[chunk_idx].empty():
            numpy_queue = move_tensor_queue_to_numpy_dict(data_queue_list[chunk_idx])
            pickle.dump(numpy_queue, open(path + str(chunk_idx) + ".fea", "wb"))
            logging.info("----------chunk_idx = %d save successfully----------" % chunk_idx)


def load_from_pickle_file(cache_path, data_queue_list, chunk_index_list_to_be_loaded):
    for chunk_idx in chunk_index_list_to_be_loaded:
        loading_path = cache_path + str(chunk_idx) + ".fea"
        if path.exists(loading_path):
            data_dict = pickle.load(open(loading_path, "rb"))
            logging.info("--------chunk_idx = %d, data_dict.size() = %d" % (chunk_idx, len(data_dict)))
            move_numpy_dict_to_tensor_queue(data_queue_list, chunk_idx, data_dict)
            os.remove(loading_path)
            logging.info("----------chunk_idx = %d load successfully----------" % (chunk_idx))


def clear_all_cache_files(cache_path, chunk_num):
    for chunk_idx in range(chunk_num):
        loading_path = cache_path + str(chunk_idx) + ".fea"
        if path.exists(loading_path):
            os.remove(loading_path)



class TwoLevelCache:
    MSG_TYPE_WRITING = 0
    MSG_TYPE_WRITING_HOST_MEMORY_FULL = 1
    MSG_TYPE_WRITING_DISK_MEMORY_FULL = 2
    MSG_TYPE_READING = 3
    MSG_TYPE_TERMINATE = 4

    def __init__(self):
        self.cache_path = "./hidden_feature_cache_" + str(id(self))
        self.is_enable = False

        self.is_cache_ready = False

        self.batch_size = -1
        self.hidden_feature_size = -1
        self.chunk_size = -1
        self.chunk_num = -1

        self.host_memory_percentage = 0.2
        self.disk_memroy_percentage = 0.2

        self.chunk_idx = 0

        self.data_q = dict()

        self.chunk_idx_starting_disk_cache = -1
        self.chunk_idx_starting_recompute = -1

        self.msg_q = mp.Queue()
        self.disk_storage_process = mp.Process(target=disk_cache_process_impl,
                                               args=(self.cache_path, self.data_q, self.msg_q))
        self.disk_storage_process.daemon = True
        self.disk_storage_process.start()
        logging.info("main process - chunk_size = %d" % self.chunk_size)

    def reset_status(self, is_ready, batch_size, hidden_feature_size, processes_num):
        self.is_cache_ready = is_ready
        self.batch_size = batch_size
        self.hidden_feature_size = hidden_feature_size
        self.chunk_size = math.floor(4e9 / hidden_feature_size)
        self.chunk_num = math.ceil(self.batch_size / self.chunk_size)
        logging.info("self.chunk_size = %d, self.chunk_num  = %d" % (self.chunk_size, self.chunk_num))

        for c_i in range(self.chunk_num):
            data_q_i = mp.Queue()
            self.data_q[c_i] = data_q_i

        self.host_memory_percentage = 0.8
        self.disk_memroy_percentage = 0.8

    def get_hidden_feature(self, batch_idx, x, model):
        if not self.is_cache_ready:
            hidden_feature = self.write_one_batch(batch_idx, x, model)
        else:
            hidden_feature = self.read_one_batch(batch_idx, x, model)
        return hidden_feature

    def write_one_batch(self, batch_idx, x, model):
        logging.info("self.chunk_size = %d, self.chunk_num  = %d" % (
            self.chunk_size, self.chunk_num))

        chunk_idx = math.floor(batch_idx / self.chunk_size)
        logging.info("main process - chunk_idx = %d" % chunk_idx)

        hidden_feature = model(x).detach().cpu()

        # one case is that the disk memory is full but the host memory is still available
        if not self.is_host_memory_full() and not self.is_disk_storage_full() or not self.is_host_memory_full() and self.is_disk_storage_full():
            logging.info("####################both are not full. chunk_idx = %d" % chunk_idx)

            self.data_q[chunk_idx].put(hidden_feature)
            if self.chunk_idx != chunk_idx:
                self.chunk_idx = chunk_idx
                msg_params = dict()
                msg_params['chunk_idx'] = chunk_idx
                msg_params['chunk_num'] = self.chunk_num
                self.msg_q.put((TwoLevelCache.MSG_TYPE_WRITING, msg_params))

        elif self.is_host_memory_full() and not self.is_disk_storage_full():
            logging.info(
                "####################Host memory is full but disk storage is available. chunk_idx = %d" % chunk_idx)
            self.data_q[chunk_idx].put(hidden_feature)
            msg_params = dict()
            if self.chunk_idx_starting_disk_cache == -1:
                self.chunk_idx_starting_disk_cache = chunk_idx
                msg_params['chunk_idx'] = self.chunk_idx_starting_disk_cache
                msg_params['chunk_num'] = self.chunk_num
                self.msg_q.put((TwoLevelCache.MSG_TYPE_WRITING_HOST_MEMORY_FULL, msg_params))

            if self.chunk_idx != chunk_idx:
                self.chunk_idx = chunk_idx
                msg_params['chunk_idx'] = self.chunk_idx
                msg_params['chunk_num'] = self.chunk_num
                self.msg_q.put((TwoLevelCache.MSG_TYPE_WRITING, msg_params))
        elif self.is_host_memory_full() and self.is_disk_storage_full():
            logging.info("####################Host memory and disk memory are all full, "
                         "will do recompute. chunk_idx = %d" % chunk_idx)
            msg_params = dict()
            if self.chunk_idx_starting_recompute == -1:
                self.chunk_idx_starting_recompute = chunk_idx
                msg_params['chunk_idx'] = self.chunk_idx_starting_recompute
                msg_params['chunk_num'] = self.chunk_num
                while not self.data_q[chunk_idx].empty():
                    self.data_q[chunk_idx].get()
                self.msg_q.put((TwoLevelCache.MSG_TYPE_WRITING_DISK_MEMORY_FULL, msg_params))
        else:
            raise Exception("edge case!")
        return hidden_feature

    def read_one_batch(self, batch_idx, x, model):
        chunk_idx = math.floor(batch_idx / self.chunk_size)
        logging.info("main process - chunk_idx = %d" % chunk_idx)
        hidden_feature = None
        if self.chunk_idx != chunk_idx:
            self.chunk_idx = chunk_idx
            msg_params = dict()
            msg_params['chunk_idx'] = chunk_idx
            msg_params['chunk_num'] = self.chunk_num
            self.msg_q.put((TwoLevelCache.MSG_TYPE_READING, msg_params))

        if chunk_idx < self.chunk_idx_starting_recompute or self.chunk_idx_starting_disk_cache == -1 and self.chunk_idx_starting_recompute == -1:
            time_starting_wait = time.time()
            """
                Waiting for the disk storage loading. 4 seconds is the time that is used to load a 4GB pickle file.
            """
            while self.data_q[chunk_idx].empty():

                time.sleep(0.1)
                logging.info("wait for loading = %f s" % (time.time() - time_starting_wait))

                if time.time() - time_starting_wait > 4.0:
                    break
            if not self.data_q[chunk_idx].empty():
                hidden_feature = self.data_q[chunk_idx].get()
        else:
            logging.info(
                "####################################################chunk_idx = %d needs to recompute" % chunk_idx)
            hidden_feature = model(x)
        if hidden_feature is None:
            raise Exception("Edge Case!")
        return hidden_feature

    def is_disk_storage_full(self):
        total, used, free = shutil.disk_usage(__file__)
        used_storage_percentage = used / total
        # logging.info("is_disk_storage_full. Percentage = " + str(used_storage_percentage))
        return True if used_storage_percentage > self.disk_memroy_percentage else False

    def is_host_memory_full(self):
        memory_cost_percent = 1 - psutil.virtual_memory()[4] / psutil.virtual_memory()[0]
        # logging.info("is_host_memory_full. Percentage = " + str(memory_cost_percent))
        return True if memory_cost_percent > self.host_memory_percentage else False

    def finish(self):
        time.sleep(5)
        msg_params = dict()
        msg_params['chunk_idx'] = 0
        msg_params['chunk_num'] = self.chunk_num
        self.msg_q.put((TwoLevelCache.MSG_TYPE_TERMINATE, msg_params))

        self.disk_storage_process.join()
        self.disk_storage_process.terminate()
        clear_all_cache_files(self.cache_path, self.chunk_num)


class Trainer:
    def __init__(self, two_level_cache, model):
        self.auto_cache = two_level_cache
        self.model = model

        self.batch_size_train = math.ceil(200000 / 512)
        self.batch_size_test = 200

    def train(self):
        x = torch.randn(20, 10)
        for batch_idx in range(self.batch_size_train):
            hidden_feature = auto_cache.write_one_batch(batch_idx, x, self.model)

        logging.info("---------------------\n")
        logging.info("---------------------\n")
        logging.info("---------------------\n")
        for batch_idx in range(self.batch_size_train):
            hidden_feature = auto_cache.read_one_batch(batch_idx, x, self.model)


class TestModel(torch.nn.Module):
    def __init__(self):
        super(TestModel, self).__init__()
        self.net1 = torch.nn.Linear(10, 10)
        self.relu = torch.nn.ReLU()
        self.net2 = torch.nn.Linear(10, 5)

    def forward(self, x):
        x = self.relu(self.net1(x))
        return self.net2(x)


if __name__ == '__main__':
    wandb.init(project="pipe_and_ddp",
               name="PipeTransformer-Cache Test")
    # customize the log format
    logging.basicConfig(level=logging.INFO,
                        format='%(processName)s - %(asctime)s.%(msecs)03d - {%(module)s.py (%(lineno)d)} - %(funcName)s(): %(message)s',
                        datefmt='%Y-%m-%d,%H:%M:%S')
    # from data loader
    batch_size = 800

    hidden_feature_size = 512 * 769 * 197 * 4

    auto_cache = TwoLevelCache()
    auto_cache.reset_status(False, batch_size, hidden_feature_size)
    auto_cache.reset_status(False, batch_size, hidden_feature_size)

    model = TestModel()
    trainer = Trainer(auto_cache, model)
    trainer.train()

    wandb.finish()
