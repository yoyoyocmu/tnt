# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# ignore errors due to `Any` type
# pyre-ignore-all-errors[2]
# pyre-ignore-all-errors[3]

from abc import ABC, abstractmethod
from typing import Any, Optional, Tuple, Union

import torch
from torch.cuda.amp import GradScaler
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from torch.distributed.fsdp.sharded_grad_scaler import ShardedGradScaler
from torchtnt.runner.state import State
from torchtnt.runner.unit import TrainUnit, TTrainData
from torchtnt.utils import copy_data_to_device, get_device_from_env
from typing_extensions import Literal


class AutoTrainUnit(TrainUnit[TTrainData], ABC):
    """
    The AutoTrainUnit is a convenience for users who are training with stochastic gradient descent and would like to have model optimization
    handled for them. The AutoTrainUnit subclasses TrainUnit, and runs the train_step for the user, specifically: forward pass, loss computation,
    backward pass, and optimizer step. To benefit from the AutoTrainUnit, the user must subclass it and implement the `compute_loss` method, and
    optionally the `update_metrics` and `log_metrics` methods. Then use with the `train` or `fit` entry point as normal.

    For more advanced customization, the basic TrainUnit interface may be a better fit.

    Args:
        module: module to be used during training.
        optimizer: optimizer to be used during training.
        lr_scheduler: lr_scheduler to be used during training.
        step_lr_interval: whether to step lr_scheduler every step or every epoch. Defaults to every epoch.
        device: the device to be used.
        log_frequency_steps: how often to log in terms of steps (parameter updates)
        precision: the precision to use in training, as either a string or a torch.dtype.

    Attributes:
        module: module to be used during training.
        optimizer: optimizer to be used during training.
        lr_scheduler: lr_scheduler to be used during training.
        step_lr_interval: whether to step lr_scheduler every step or every epoch. Defaults to every epoch.
        device: the device to be used.
        log_frequency_steps: how often to log in terms of steps (parameter updates)
        precision: the precision to use in training, as a torch.dtype.
        grad_scaler: a torch.cuda.amp.GradScaler, if using fp16 precision
    """

    def __init__(
        self,
        *,
        module: torch.nn.Module,
        optimizer: torch.optim.Optimizer,
        lr_scheduler: Optional[torch.optim.lr_scheduler._LRScheduler] = None,
        step_lr_interval: Literal["step", "epoch"] = "epoch",
        device: Optional[torch.device] = None,
        log_frequency_steps: int,
        precision: Optional[Union[str, torch.dtype]] = None,
    ) -> None:
        super().__init__()
        self.module = module
        self.optimizer = optimizer
        self.lr_scheduler = lr_scheduler
        self.step_lr_interval = step_lr_interval
        self.device: torch.device = device or get_device_from_env()
        self.log_frequency_steps: int = log_frequency_steps

        if not precision:
            self.precision: Optional[torch.dtype] = None
            self.grad_scaler: Optional[GradScaler] = None
        else:
            if isinstance(precision, str):
                self.precision: Optional[torch.dtype] = _convert_precision_str_to_dtype(
                    precision
                )
            else:
                self.precision = precision

            self.grad_scaler = _get_grad_scaler_from_precision(
                # pyre-ignore
                self.precision,
                self.module,
            )

        # TODO: Make AutoTrainUnit work when data type is Iterator

    @abstractmethod
    def compute_loss(self, state: State, data: TTrainData) -> Tuple[torch.Tensor, Any]:
        """
        The user should implement this method with their loss computation. This will be called every `train_step`.

        Args:
            state: a State object which is passed from the `train_step`
            data: a batch of data which is passed from the `train_step`

        Returns:
            Tuple containing the loss and the output of the model
        """
        ...

    def update_metrics(
        self, state: State, data: TTrainData, loss: torch.Tensor, outputs: Any
    ) -> None:
        """
        The user should implement this method with code to update metrics. This will be called every `train_step`.

        Args:
            state: a State object which is passed from the `train_step`
            data: a batch of data which is passed from the `train_step`
            outputs: the outputs of the model forward pass
        """
        pass

    def log_metrics(
        self, state: State, step: int, interval: Literal["step", "epoch"]
    ) -> None:
        """
        The user should implement this method with their code to log metrics. This will be called based on `log_frequency_steps`
        and how many parameter updates have been run on the model.

        Args:
            state: a State object which is passed from the `train_step`
            step: how many steps have been completed (i.e. how many parameter updates have been run on the model)
            interval: whether `log_metrics` is called at the end of a step or at the end of an epoch
        """
        pass

    def train_step(self, state: State, data: TTrainData) -> Tuple[torch.Tensor, Any]:
        data = copy_data_to_device(data, self.device)
        # users must override this
        loss, outputs = self.compute_loss(state, data)
        maybe_autocast_precision = torch.autocast(
            device_type=self.device.type,
            dtype=self.precision,
            enabled=self.precision is not None,
        )

        with maybe_autocast_precision:
            loss, outputs = self.compute_loss(state, data)

        grad_scaler = self.grad_scaler
        if grad_scaler:
            loss = grad_scaler.scale(loss)
        loss.backward()

        # optimizer step
        if grad_scaler:
            grad_scaler.step(self.optimizer)
            # update the scale for next iteration
            grad_scaler.update()
        else:
            self.optimizer.step()

        # sets gradients to zero
        self.optimizer.zero_grad(set_to_none=True)

        if self.lr_scheduler and self.step_lr_interval == "step":
            self.lr_scheduler.step()

        # users can override this, by default this is a no-op
        self.update_metrics(state, data, loss, outputs)

        assert state.train_state
        step_count = state.train_state.progress.num_steps_completed
        if (step_count + 1) % self.log_frequency_steps == 0:
            # users can override this, by default this is a no-op
            self.log_metrics(state, step_count, "step")

        return loss, outputs

    def on_train_epoch_end(self, state: State) -> None:
        # step the learning rate scheduler
        # note: if user wants to override on_train_epoch_end themselves, they should remember to call up to this method via super().on_train_epoch_end()
        if self.lr_scheduler and self.step_lr_interval == "epoch":
            self.lr_scheduler.step()

        assert state.train_state
        step_count = state.train_state.progress.num_steps_completed
        # users can override this, by default this is a no-op
        self.log_metrics(state, step_count, "epoch")


def _convert_precision_str_to_dtype(precision: str) -> torch.dtype:
    """
    Converts precision as a string to a torch.dtype

    Args:
        precision: string containing the precision

    Raises:
        ValueError if an invalid precision string is passed.

    """
    string_to_dtype_mapping = {"fp16": torch.float16, "bf16": torch.bfloat16}
    if precision not in string_to_dtype_mapping.keys():
        raise ValueError(
            f"Precision {precision} not supported. Please use one of `fp16` or `bf16`"
        )
    return string_to_dtype_mapping[precision]


def _get_grad_scaler_from_precision(
    precision: torch.dtype, module: torch.nn.Module
) -> Optional[GradScaler]:
    if precision == torch.float16:
        if isinstance(module, FSDP):
            return ShardedGradScaler()
        else:
            return GradScaler()
    return None