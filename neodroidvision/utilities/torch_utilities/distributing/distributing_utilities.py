#!/usr/bin/env python3
# -*- coding: utf-8 -*-

__author__ = "Christian Heider Nielsen"
__doc__ = r"""

           Created on 01/03/2020
           """

import logging
import os
import pickle
import sys
from pathlib import Path
from typing import List

import torch
import torch.utils.data
from torch import distributed

__all__ = [
    "all_gather",
    "reduce_dict",
    "setup_for_distributed",
    "is_distribution_available_and_initialized",
    "is_main_process",
    "init_distributed_mode",
    "save_on_master",
    "global_distribution_rank",
    "global_world_size",
    "set_benchmark_device_dist",
    "synchronise_torch_barrier",
]


def is_distribution_available_and_initialized() -> bool:
    if not distributed.is_available():
        return False
    if not distributed.is_initialized():
        return False
    return True


def global_world_size() -> int:
    if not is_distribution_available_and_initialized():
        return 1
    return distributed.get_world_size()


def global_distribution_rank() -> int:
    if not is_distribution_available_and_initialized():
        return 0
    return distributed.get_rank()


def is_main_process() -> bool:
    return global_distribution_rank() == 0


def save_on_master(*args, **kwargs) -> None:
    if is_main_process():
        torch.save(*args, **kwargs)


def setup_for_distributed(is_master: bool) -> None:
    """
This function disables printing when not in master process
"""
    import builtins as __builtin__

    builtin_print = __builtin__.print

    def print(*args, **kwargs):
        force = kwargs.pop("force", False)
        if is_master or force:
            builtin_print(*args, **kwargs)

    __builtin__.print = print


def all_gather(data) -> List[bytes]:
    """
Run all_gather on arbitrary picklable data (not necessarily tensors)
Args:
    data: any picklable object
Returns:
    list[data]: list of data gathered from each rank
"""
    world_size = global_world_size()
    if world_size == 1:
        return [data]

    # serialized to a Tensor
    buffer = pickle.dumps(data)
    storage = torch.ByteStorage.from_buffer(buffer)
    tensor = torch.ByteTensor(storage).to("cuda")

    # obtain Tensor size of each rank
    local_size = torch.tensor([tensor.numel()], device="cuda")
    size_list = [torch.tensor([0], device="cuda") for _ in range(world_size)]
    distributed.all_gather(size_list, local_size)
    size_list = [int(size.item()) for size in size_list]
    max_size = max(size_list)

    # receiving Tensor from all ranks
    # we pad the tensor because torch all_gather does not support
    # gathering tensors of different shapes
    tensor_list = []
    for _ in size_list:
        tensor_list.append(torch.empty((max_size,), dtype=torch.uint8, device="cuda"))
    if local_size != max_size:
        padding = torch.empty(
            size=(max_size - local_size,), dtype=torch.uint8, device="cuda"
        )
        tensor = torch.cat((tensor, padding), dim=0)
    distributed.all_gather(tensor_list, tensor)

    data_list = []
    for size, tensor in zip(size_list, tensor_list):
        buffer = tensor.cpu().numpy().tobytes()[:size]
        data_list.append(pickle.loads(buffer))

    return data_list


def reduce_dict(input_dict: dict, average: bool = True) -> dict:
    """
Args:
    input_dict (dict): all the values will be reduced
    average (bool): whether to do average or sum
Reduce the values in the dictionary from all processes so that all processes
have the averaged results. Returns a dict with the same fields as
input_dict, after reduction.
"""
    world_size = global_world_size()
    if world_size < 2:
        return input_dict
    with torch.no_grad():
        names = []
        values = []
        # sort the keys so that they are consistent across processes
        for k in sorted(input_dict.keys()):
            names.append(k)
            values.append(input_dict[k])
        values = torch.stack(values, dim=0)
        distributed.all_reduce(values)
        if average:
            values /= world_size
        reduced_dict = {k: v for k, v in zip(names, values)}
    return reduced_dict


def init_distributed_mode(args) -> None:
    if "RANK" in os.environ and "WORLD_SIZE" in os.environ:
        args.rank = int(os.environ["RANK"])
        args.world_size = int(os.environ["WORLD_SIZE"])
        args.gpu = int(os.environ["LOCAL_RANK"])
    elif "SLURM_PROCID" in os.environ:
        args.rank = int(os.environ["SLURM_PROCID"])
        args.gpu = args.rank % torch.cuda.device_count()
    else:
        print("Not using distributed mode")
        args.distributed = False
        return

    args.distributed = True

    torch.cuda.set_device(args.gpu)
    args.dist_backend = "nccl"
    print(f"| distributed init (rank {args.rank}): {args.dist_url}", flush=True)
    torch.distributed.init_process_group(
        backend=args.dist_backend,
        init_method=args.dist_url,
        world_size=args.world_size,
        rank=args.rank,
    )
    torch.distributed.barrier()
    setup_for_distributed(args.rank == 0)


def synchronise_torch_barrier() -> None:
    """
     Helper function to synchronize (barrier) among all processes when
     using distributed training
  """
    if not distributed.is_available():
        return
    if not distributed.is_initialized():
        return
    world_size = distributed.get_world_size()
    if world_size == 1:
        return
    distributed.barrier()


def torch_byte_tensor_encode(encoded_data, data) -> None:
    # gets a byte representation for the data
    encoded_bytes = pickle.dumps(data)
    # convert this byte string into a byte tensor
    storage = torch.ByteStorage.from_buffer(encoded_bytes)
    tensor = torch.ByteTensor(storage).to("cuda")
    # encoding: first byte is the size and then rest is the data
    s = tensor.numel()
    assert s <= 255, "Can't encode data greater than 255 bytes"
    # put the encoded data in encoded_data
    encoded_data[0] = s
    encoded_data[1 : (s + 1)] = tensor


def setup_distributed_logger(
    name: str, distributed_rank: int, save_dir: Path = None
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    # don't log results for the non-master process
    if distributed_rank > 0:
        return logger
    stream_handler = logging.StreamHandler(stream=sys.stdout)
    stream_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s")
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    if save_dir:
        fh = logging.FileHandler(str(save_dir / "log.txt"))
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    return logger


def set_benchmark_device_dist(distributed: bool, local_rank: int) -> None:
    if torch.cuda.is_available():
        # This flag allows you to enable the inbuilt cudnn auto-tuner to
        # find the best algorithm to use for your hardware.
        torch.backends.cudnn.benchmark = True
    if distributed:
        torch.cuda.set_device(local_rank)
        torch.distributed.init_process_group(backend="nccl", init_method="env://")
        synchronise_torch_barrier()