#!/usr/bin/env python3

# Original work Copyright (c) Facebook, Inc. and its affiliates.
# Modified work Copyright (c) Allen Institute for AI
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from multiprocessing.connection import Connection
from multiprocessing.context import BaseContext
from queue import Queue
from threading import Thread
from typing import Any, Callable, List, Optional, Sequence, Set, Tuple, Union, Dict

import numpy as np
from gym.spaces.dict import Dict as SpaceDict

from rl_base.task import TaskSampler

try:
    # Use torch.multiprocessing if we can.
    # We have yet to find a reason to not use it and
    # you are required to use it when sending a torch.Tensor
    # between processes
    import torch.multiprocessing as mp
except ImportError:
    import multiprocessing as mp

STEP_COMMAND = "step"
NEXT_TASK_COMMAND = "next_task"
RENDER_COMMAND = "render"
CLOSE_COMMAND = "close"
OBSERVATION_SPACE_COMMAND = "observation_space"
ACTION_SPACE_COMMAND = "action_space"
CALL_COMMAND = "call"
# EPISODE_COMMAND = "current_episode"


def tile_images(images: List[np.ndarray]) -> np.ndarray:
    r"""Tile multiple images into single image

    Args:
        images: list of images where each image has dimension
            (height x width x channels)

    Returns:
        tiled image (new_height x width x channels)
    """
    assert len(images) > 0, "empty list of images"
    np_images = np.asarray(images)
    n_images, height, width, n_channels = np_images.shape
    new_height = int(np.ceil(np.sqrt(n_images)))
    new_width = int(np.ceil(float(n_images) / new_height))
    # pad with empty images to complete the rectangle
    np_images = np.array(
        images + [images[0] * 0 for _ in range(n_images, new_height * new_width)]
    )
    # img_HWhwc
    out_image = np_images.reshape((new_height, new_width, height, width, n_channels))
    # img_HhWwc
    out_image = out_image.transpose(0, 2, 1, 3, 4)
    # img_Hh_Ww_c
    out_image = out_image.reshape((new_height * height, new_width * width, n_channels))
    return out_image


class VectorSampledTasks:
    """Vectorized collection of tasks. Creates multiple processes where each
    process runs its own TaskSampler. Each process generates one Task from it's
    TaskSampler at a time and this class allows for interacting with these
    tasks in a vectorized manner. When a task on a process completes, the
    process samples another task from its task sampler. All the tasks are
    synchronized (for step and reset methods).

    Args:
        make_sampler_fn: function which creates a single TaskSampler.
        sampler_fn_args: sequence of dictionaries describing the args
            to pass to make_sampler_fn on each individual process.
        auto_resample_when_done: automatically sample a new Task from the TaskSampler when
            the Task completes. If False, a new Task will not be resampled until all
            Tasks on all processes have completed. This functionality is provided for seamless training
            of vectorized Tasks.
        multiprocessing_start_method: the multiprocessing method used to
            spawn worker processes. Valid methods are
            ``{'spawn', 'forkserver', 'fork'}`` ``'forkserver'`` is the
            recommended method as it works well with CUDA. If
            ``'fork'`` is used, the subproccess  must be started before
            any other GPU useage.
    """

    observation_spaces: List[SpaceDict]
    _workers: List[Union[mp.Process, Thread]]
    _is_waiting: bool
    _num_processes: int
    _auto_resample_when_done: bool
    _mp_ctx: BaseContext
    _connection_read_fns: List[Callable[[], Any]]
    _connection_write_fns: List[Callable[[Any], None]]

    def __init__(
        self,
        make_sampler_fn: Callable[..., TaskSampler],
        sampler_fn_args: Sequence[Dict[str, Any]] = None,
        auto_resample_when_done: bool = True,
        multiprocessing_start_method: str = "forkserver",
    ) -> None:

        self._is_waiting = False
        self._is_closed = True

        assert (
            sampler_fn_args is not None and len(sampler_fn_args) > 0
        ), "number of processes to be created should be greater than 0"

        self._num_processes = len(sampler_fn_args)

        assert multiprocessing_start_method in self._valid_start_methods, (
            "multiprocessing_start_method must be one of {}. Got '{}'"
        ).format(self._valid_start_methods, multiprocessing_start_method)
        self._auto_resample_when_done = auto_resample_when_done
        self._mp_ctx = mp.get_context(multiprocessing_start_method)
        self._workers = []
        (
            self._connection_read_fns,
            self._connection_write_fns,
        ) = self._spawn_workers(  # noqa
            make_sampler_fn=make_sampler_fn,
            sampler_fn_args=[
                {"mp_ctx": self._mp_ctx, **args} for args in sampler_fn_args
            ],
        )

        self._is_closed = False

        for write_fn in self._connection_write_fns:
            write_fn((OBSERVATION_SPACE_COMMAND, None))
        self.observation_spaces = [read_fn() for read_fn in self._connection_read_fns]
        for write_fn in self._connection_write_fns:
            write_fn((ACTION_SPACE_COMMAND, None))
        self.action_spaces = [read_fn() for read_fn in self._connection_read_fns]
        self._paused = []

    @property
    def num_unpaused_tasks(self):
        """
        Returns:
             number of individual unpaused processes.
        """
        return self._num_processes - len(self._paused)

    @staticmethod
    def _task_sampling_loop_worker(
        worker_id: int,
        connection_read_fn: Callable,
        connection_write_fn: Callable,
        make_sampler_fn: Callable[..., TaskSampler],
        sampler_fn_args: Dict[str, Any],
        auto_resample_when_done: bool,
        child_pipe: Optional[Connection] = None,
        parent_pipe: Optional[Connection] = None,
    ) -> None:
        """process worker for creating and interacting with the
        Tasks/TaskSampler."""
        task_sampler = make_sampler_fn(**sampler_fn_args)
        current_task = task_sampler.next_task()

        if parent_pipe is not None:
            parent_pipe.close()
        try:
            command, data = connection_read_fn()
            while command != CLOSE_COMMAND:
                if command == STEP_COMMAND:
                    step_result = current_task.step(data)
                    if auto_resample_when_done and current_task.is_done():
                        current_task = task_sampler.next_task()
                        step_result.observations = current_task.get_observations()

                    connection_write_fn(step_result)

                elif command == NEXT_TASK_COMMAND:
                    current_task = task_sampler.next_task()
                    observations = current_task.get_observations()
                    connection_write_fn(observations)

                elif command == RENDER_COMMAND:
                    connection_write_fn(current_task.render(*data[0], **data[1]))

                elif (
                    command == OBSERVATION_SPACE_COMMAND
                    or command == ACTION_SPACE_COMMAND
                ):
                    connection_write_fn(getattr(current_task, command))

                elif command == CALL_COMMAND:
                    function_name, function_args = data
                    if function_args is None or len(function_args) == 0:
                        result = getattr(current_task, function_name)()
                    else:
                        result = getattr(current_task, function_name)(*function_args)
                    connection_write_fn(result)

                # TODO: update CALL_COMMAND for getting attribute like this
                # elif command == EPISODE_COMMAND:
                #     connection_write_fn(current_task.current_episode)
                else:
                    raise NotImplementedError()

                command, data = connection_read_fn()

            if child_pipe is not None:
                child_pipe.close()
        except KeyboardInterrupt:
            # logger.info("Worker KeyboardInterrupt")
            print("Worker {} KeyboardInterrupt".format(worker_id))
        finally:
            """Worker {} closing.""".format(worker_id)
            task_sampler.close()

    def _spawn_workers(
        self,
        make_sampler_fn: Callable[..., TaskSampler],
        sampler_fn_args: Sequence[Dict[str, Any]],
    ) -> Tuple[List[Callable[[], Any]], List[Callable[[Any], None]]]:
        parent_connections, worker_connections = zip(
            *[self._mp_ctx.Pipe(duplex=True) for _ in range(self._num_processes)]
        )
        self._workers = []
        for worker_conn, parent_conn, sampler_fn_args in zip(
            worker_connections, parent_connections, sampler_fn_args
        ):
            # noinspection PyUnresolvedReferences
            ps = self._mp_ctx.Process(
                target=self._task_sampling_loop_worker,
                args=(
                    worker_conn.recv,
                    worker_conn.send,
                    make_sampler_fn,
                    sampler_fn_args,
                    self._auto_resample_when_done,
                    worker_conn,
                    parent_conn,
                ),
            )
            self._workers.append(ps)
            ps.daemon = True
            ps.start()
            worker_conn.close()
        return (
            [p.recv for p in parent_connections],
            [p.send for p in parent_connections],
        )

    # def current_episodes(self):
    #     self._is_waiting = True
    #     for write_fn in self._connection_write_fns:
    #         write_fn((EPISODE_COMMAND, None))
    #     results = []
    #     for read_fn in self._connection_read_fns:
    #         results.append(read_fn())
    #     self._is_waiting = False
    #     return results

    def next_task(self):
        """Move to the the next Task for all TaskSamplers.

        Returns:
            list of initial observations for each of the new tasks.
        """
        self._is_waiting = True
        for write_fn in self._connection_write_fns:
            write_fn((NEXT_TASK_COMMAND, None))
        results = []
        for read_fn in self._connection_read_fns:
            results.append(read_fn())
        self._is_waiting = False
        return results

    def next_task_at(self, index_process: int):
        """Move to the the next Task from the TaskSampler in index_process
        process in the vector.

        Args:
            index_process: index of the process to be reset

        Returns:
            list of length one containing the observations the newly sampled task.
        """
        self._is_waiting = True
        self._connection_write_fns[index_process]((NEXT_TASK_COMMAND, None))
        results = [self._connection_read_fns[index_process]()]
        self._is_waiting = False
        return results

    def step_at(self, index_process: int, action: int):
        """Step in the index_process task in the vector.

        Args:
            index_process: index of the Task to be stepped in
            action: action to be taken

        Returns:
            list containing the output of step method on the task in the indexed process.
        """
        self._is_waiting = True
        self._connection_write_fns[index_process]((STEP_COMMAND, action))
        results = [self._connection_read_fns[index_process]()]
        self._is_waiting = False
        return results

    def async_step(self, actions: List[int]) -> None:
        """Asynchronously step in the vectorized Tasks.

        Args:
            actions: actions to be performed in the vectorized Tasks.
        """
        self._is_waiting = True
        for write_fn, action in zip(self._connection_write_fns, actions):
            write_fn((STEP_COMMAND, action))

    def wait_step(self) -> List[Dict[str, Any]]:
        """Wait until all the asynchronized processes have synchronized."""
        observations = []
        for read_fn in self._connection_read_fns:
            observations.append(read_fn())
        self._is_waiting = False
        return observations

    def step(self, actions: List[int]):
        """Perform actions in the vectorized tasks.

        Args:
            actions: list of size _num_processes containing action to be taken
                in each task.

        Returns:
            list of outputs from the step method of tasks.
        """
        self.async_step(actions)
        return self.wait_step()

    def close(self) -> None:
        if self._is_closed:
            return

        if self._is_waiting:
            for read_fn in self._connection_read_fns:
                read_fn()

        for write_fn in self._connection_write_fns:
            write_fn((CLOSE_COMMAND, None))

        for _, _, write_fn, _ in self._paused:
            write_fn((CLOSE_COMMAND, None))

        for process in self._workers:
            process.join()

        for _, _, _, process in self._paused:
            process.join()

        self._is_closed = True

    def pause_at(self, index: int) -> None:
        """Pauses computation on the Task in process `index` without destroying
        the Task. This is useful for not needing to call steps on all Tasks
        when only some are active (for example during the last samples of
        running eval).

        Args:
            index: which process to pause. All indexes after this one will be
                shifted down by one.
        """
        if self._is_waiting:
            for read_fn in self._connection_read_fns:
                read_fn()
        read_fn = self._connection_read_fns.pop(index)
        write_fn = self._connection_write_fns.pop(index)
        worker = self._workers.pop(index)
        self._paused.append((index, read_fn, write_fn, worker))

    def resume_all(self) -> None:
        """Resumes any paused processes."""
        for index, read_fn, write_fn, worker in reversed(self._paused):
            self._connection_read_fns.insert(index, read_fn)
            self._connection_write_fns.insert(index, write_fn)
            self._workers.insert(index, worker)
        self._paused = []

    def call_at(
        self, index: int, function_name: str, function_args: Optional[List[Any]] = None
    ) -> Any:
        """Calls a function (which is passed by name) on the selected task and
        returns the result.

        Args:
            index: which task to call the function on.
            function_name: the name of the function to call on the task.
            function_args: optional function args.

        Returns:
            result of calling the function.
        """
        self._is_waiting = True
        self._connection_write_fns[index](
            (CALL_COMMAND, (function_name, function_args))
        )
        result = self._connection_read_fns[index]()
        self._is_waiting = False
        return result

    def call(
        self, function_names: List[str], function_args_list: Optional[List[Any]] = None
    ) -> List[Any]:
        """Calls a list of functions (which are passed by name) on the
        corresponding task (by index).

        Args:
            function_names: the name of the functions to call on the tasks.
            function_args_list: list of function args for each function. If
                provided, len(function_args_list) should be as long as
                len(function_names).

        Returns:
            result of calling the function.
        """
        self._is_waiting = True
        if function_args_list is None:
            function_args_list = [None] * len(function_names)
        assert len(function_names) == len(function_args_list)
        func_args = zip(function_names, function_args_list)
        for write_fn, func_args_on in zip(self._connection_write_fns, func_args):
            write_fn((CALL_COMMAND, func_args_on))
        results = []
        for read_fn in self._connection_read_fns:
            results.append(read_fn())
        self._is_waiting = False
        return results

    def render(self, mode: str = "human", *args, **kwargs) -> Union[np.ndarray, None]:
        """Render observations from all Tasks in a tiled image."""
        for write_fn in self._connection_write_fns:
            write_fn((RENDER_COMMAND, (args, {"mode": "rgb", **kwargs})))
        images = [read_fn() for read_fn in self._connection_read_fns]
        tile = tile_images(images)
        if mode == "human":
            import cv2

            cv2.imshow("vectask", tile[:, :, ::-1])
            cv2.waitKey(1)
            return None
        elif mode == "rgb_array":
            return tile
        else:
            raise NotImplementedError

    @property
    def _valid_start_methods(self) -> Set[str]:
        return {"forkserver", "spawn", "fork"}

    def __del__(self):
        self.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class ThreadedVectorSampledTasks(VectorSampledTasks):
    """Provides same functionality as ``VectorSampledTasks``, the only
    difference is it runs in a multi-thread setup inside a single process.

    ``VectorSampledTasks`` runs in a multi-proc setup. This makes it
    much easier to debug when using ``VectorSampledTasks`` because you
    can actually put break points in the Task methods. It should not be
    used for best performance.
    """

    def _spawn_workers(
        self,
        make_sampler_fn: Callable[..., TaskSampler],
        sampler_fn_args: Sequence[Tuple],
    ) -> Tuple[List[Callable[[], Any]], List[Callable[[Any], None]]]:
        parent_read_queues, parent_write_queues = zip(
            *[(Queue(), Queue()) for _ in range(self._num_processes)]
        )
        self._workers = []
        for parent_read_queue, parent_write_queue, sampler_fn_args in zip(
            parent_read_queues, parent_write_queues, sampler_fn_args
        ):
            thread = Thread(
                target=self._task_sampling_loop_worker(),
                args=(
                    parent_write_queue.get,
                    parent_read_queue.put,
                    make_sampler_fn,
                    sampler_fn_args,
                    self._auto_resample_when_done,
                ),
            )
            self._workers.append(thread)
            thread.daemon = True
            thread.start()
        return (
            [q.get for q in parent_read_queues],
            [q.put for q in parent_write_queues],
        )