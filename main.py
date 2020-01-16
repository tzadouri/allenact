import os
import os
import time
from collections import deque
from typing import Any, Dict

import numpy as np
import torch

from configs.util import Builder
from evaluation import evaluate
from onpolicy_sync import losses, utils
from onpolicy_sync.arguments import get_args
from onpolicy_sync.envs import make_vec_envs
from onpolicy_sync.model import Policy
from onpolicy_sync.storage import RolloutStorage
from onpolicy_sync.vector_task import VectorSampledTasks


def train_loop(
    args, agent, actor_critic, rollouts, envs, eval_log_dir, device, start,
):
    episode_rewards = deque(maxlen=10)
    num_updates = int(args.num_env_steps) // args.num_steps // args.num_processes
    for j in range(num_updates):

        if args.use_linear_lr_decay:
            # decrease learning rate linearly
            utils.update_linear_schedule(
                agent.optimizer,
                j,
                num_updates,
                agent.optimizer.lr if args.algo == "acktr" else args.lr,
            )

        for step in range(args.num_steps):
            # Sample actions
            with torch.no_grad():
                (
                    value,
                    action,
                    action_log_prob,
                    recurrent_hidden_states,
                ) = actor_critic.act(
                    rollouts.obs[step],
                    rollouts.recurrent_hidden_states[step],
                    rollouts.masks[step],
                )

            # Obser reward and next obs
            obs, reward, done, infos = envs.step(action)

            for info in infos:
                if "episode" in info.keys():
                    episode_rewards.append(info["episode"]["r"])

            # If done then clean the history of observations.
            masks = torch.FloatTensor([[0.0] if done_ else [1.0] for done_ in done])
            bad_masks = torch.FloatTensor(
                [[0.0] if "bad_transition" in info.keys() else [1.0] for info in infos]
            )
            rollouts.insert(
                obs,
                recurrent_hidden_states,
                action,
                action_log_prob,
                value,
                reward,
                masks,
                bad_masks,
            )

        with torch.no_grad():
            next_value = actor_critic.get_value(
                rollouts.obs[-1],
                rollouts.recurrent_hidden_states[-1],
                rollouts.masks[-1],
            ).detach()

        rollouts.compute_returns(
            next_value,
            args.use_gae,
            args.gamma,
            args.gae_lambda,
            args.use_proper_time_limits,
        )

        value_loss, action_loss, dist_entropy = agent.update(rollouts)

        rollouts.after_update()

        # save for every interval-th episode or for the last epoch
        if (
            j % args.save_interval == 0 or j == num_updates - 1
        ) and args.save_dir != "":
            save_path = os.path.join(args.save_dir, args.algo)
            try:
                os.makedirs(save_path)
            except OSError:
                pass

            torch.save(
                [actor_critic, getattr(utils.get_vec_normalize(envs), "ob_rms", None)],
                os.path.join(save_path, args.env_name + ".pt"),
            )

        if j % args.log_interval == 0 and len(episode_rewards) > 1:
            total_num_steps = (j + 1) * args.num_processes * args.num_steps
            end = time.time()
            print(
                "Updates {}, num timesteps {}, FPS {} \n Last {} training episodes: mean/median reward {:.1f}/{:.1f}, min/max reward {:.1f}/{:.1f}\n".format(
                    j,
                    total_num_steps,
                    int(total_num_steps / (end - start)),
                    len(episode_rewards),
                    np.mean(episode_rewards),
                    np.median(episode_rewards),
                    np.min(episode_rewards),
                    np.max(episode_rewards),
                    dist_entropy,
                    value_loss,
                    action_loss,
                )
            )

        if (
            args.eval_interval is not None
            and len(episode_rewards) > 1
            and j % args.eval_interval == 0
        ):
            ob_rms = utils.get_vec_normalize(envs).ob_rms
            evaluate(
                actor_critic,
                ob_rms,
                args.env_name,
                args.seed,
                args.num_processes,
                eval_log_dir,
                device,
            )


def run_pipeline(
    train_pipeline: Dict[str, Any],
    policy: Policy,
    train_sampler_kwargs: Dict[str, Any],
    eval_log_dir: str,
):
    losses = dict()

    optimizer = train_pipeline["optimizer"]
    if isinstance(optimizer, Builder):
        optimizer = optimizer(
            params=[p for p in policy.parameters() if p.requires_grad]
        )

    vectask = VectorSampledTasks(make_sampler_fn=None)
    rollouts = RolloutStorage(
        train_pipeline["num_steps"],
        train_pipeline["nproccesses"],
        vectask.observation_space.shape,
        envs.action_space,
        policy.recurrent_hidden_state_size,
    )
    nsteps = 0
    start_time = time.time()
    for stage in train_pipeline["pipeline"]:
        stage_losses = dict()
        stage_weights = {name: 1.0 for name in stage["losses"]}
        for name in stage["losses"]:
            if name in losses:
                stage_losses[name] = losses[name]
            else:
                if isinstance(train_pipeline[name], Builder):
                    losses[name] = train_pipeline[name](optimizer=optimizer)
                else:
                    losses[name] = train_pipeline[name]
                stage_losses[name] = losses[name]
            stage_limit = stage["criterion"]  # - nsteps
            train_loop(
                losses,
                policy,
                rollouts,
                vectask,
                eval_log_dir,
                train_pipeline["gpu_ids"],
                start_time,
                stage_limit,
            )
            nsteps += stage_limit


def main():
    args = get_args()

    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)

    if args.cuda and torch.cuda.is_available() and args.cuda_deterministic:
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True

    log_dir = os.path.expanduser(args.log_dir)
    eval_log_dir = log_dir + "_eval"
    utils.cleanup_log_dir(log_dir)
    utils.cleanup_log_dir(eval_log_dir)

    torch.set_num_threads(1)
    device = torch.device("cuda:0" if args.cuda else "cpu")

    envs = make_vec_envs(
        args.env_name,
        args.seed,
        args.num_processes,
        args.gamma,
        args.log_dir,
        device,
        False,
    )

    actor_critic = Policy(
        envs.observation_space.shape,
        envs.action_space,
        base_kwargs={"recurrent": args.recurrent_policy},
    )
    actor_critic.to(device)

    if args.algo == "a2c":
        agent = losses.A2C(
            args.value_loss_coef,
            args.entropy_coef,
            optimizer,
            max_grad_norm=args.max_grad_norm,
        )
    elif args.algo == "ppo":
        agent = losses.PPO(
            args.clip_param,
            args.ppo_epoch,
            args.num_mini_batch,
            args.value_loss_coef,
            args.entropy_coef,
            optimizer,
            max_grad_norm=args.max_grad_norm,
        )
    elif args.algo == "acktr":
        agent = losses.ACKTR(args.value_loss_coef, args.entropy_coef, optimizer)

    rollouts = RolloutStorage(
        args.num_steps,
        args.num_processes,
        envs.observation_space.shape,
        envs.action_space,
        actor_critic.recurrent_hidden_state_size,
    )

    rollouts.to(device)

    start = time.time()
    train_loop(
        args, agent, actor_critic, rollouts, envs, eval_log_dir, device, start,
    )


if __name__ == "__main__":
    main()
