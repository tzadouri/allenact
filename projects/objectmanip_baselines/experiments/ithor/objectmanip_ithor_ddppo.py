import gym
import torch.nn as nn
import torch.optim as optim
from torch.optim.lr_scheduler import LambdaLR
from torchvision import models

from core.algorithms.onpolicy_sync.losses import PPO
from core.algorithms.onpolicy_sync.losses.ppo import PPOConfig
from projects.objectmanip_baselines.experiments.ithor.objectmanip_ithor_base import (
    ObjectManipThorBaseConfig,
)
from projects.objectmanip_baselines.models.object_manip_models import (
    ObjectManipBaselineActorCritic,
)
from plugins.ithor_plugin.ithor_sensors import (RGBSensorThor, GoalObjectTypeThorSensor,
                                                ArmCollisionSensor, CurrentArmStateThorSensor)

from plugins.habitat_plugin.habitat_preprocessors import ResnetPreProcessorHabitat
from plugins.ithor_plugin.ithor_tasks import ObjectManipTask
from utils.experiment_utils import Builder, PipelineStage, TrainingPipeline, LinearDecay

class ObjectManipThorRGBPPOExperimentConfig(ObjectManipThorBaseConfig):
    """An Object Navigation experiment configuration in iThor with RGB
    input."""

    def __init__(self):
        super().__init__()
        self.SENSORS = [
            RGBSensorThor(
                height=self.SCREEN_SIZE,
                width=self.SCREEN_SIZE,
                use_resnet_normalization=True,
                uuid="rgb_lowres",
            ),
            GoalObjectTypeThorSensor(object_types=self.TARGET_TYPES,),
            ArmCollisionSensor(),
            CurrentArmStateThorSensor(),
        ]

        self.PREPROCESSORS = [
            Builder(
                ResnetPreProcessorHabitat,
                {
                    "input_height": self.SCREEN_SIZE,
                    "input_width": self.SCREEN_SIZE,
                    "output_width": 7,
                    "output_height": 7,
                    "output_dims": 512,
                    "pool": False,
                    "torchvision_resnet_model": models.resnet18,
                    "input_uuids": ["rgb_lowres"],
                    "output_uuid": "rgb_resnet",
                    "parallel": False,
                },
            ),
        ]

        self.OBSERVATIONS = [
            "rgb_resnet",
            "goal_object_type_ind",
            "current_arm_state",
            "arm_collision_state",
        ]

    @classmethod
    def tag(cls):
        return "Objectmanip-iTHOR-DDPPO"

    def training_pipeline(self, **kwargs):
        ppo_steps = int(1e7)
        lr = 3e-4
        num_mini_batch = 1
        update_repeats = 3
        num_steps = 128
        save_interval = 100000
        log_interval = 5000
        gamma = 0.99
        use_gae = True
        gae_lambda = 0.95
        max_grad_norm = 0.5
        
        return TrainingPipeline(
            save_interval=save_interval,
            metric_accumulate_interval=log_interval,
            optimizer_builder=Builder(optim.Adam, dict(lr=lr)),
            num_mini_batch=num_mini_batch,
            update_repeats=update_repeats,
            max_grad_norm=max_grad_norm,
            num_steps=num_steps,
            named_losses={"ppo_loss": Builder(PPO, kwargs={}, default=PPOConfig,)},
            gamma=gamma,
            use_gae=use_gae,
            gae_lambda=gae_lambda,
            advance_scene_rollout_period=self.ADVANCE_SCENE_ROLLOUT_PERIOD,
            pipeline_stages=[
                PipelineStage(loss_names=["ppo_loss"], max_stage_steps=ppo_steps)
            ],
            lr_scheduler_builder=Builder(
                LambdaLR, {"lr_lambda": LinearDecay(steps=ppo_steps)}
            ),
        )

    @classmethod
    def create_model(cls, **kwargs) -> nn.Module:
        return ObjectManipBaselineActorCritic(
            action_space=gym.spaces.Discrete(len(ObjectManipTask.class_action_names())),
            observation_space=kwargs["observation_set"].observation_spaces,
            goal_sensor_uuid="goal_object_type_ind",
            arm_collision_uuid="arm_collision_state",
            arm_state_uuid="current_arm_state",
            hidden_size=512,
            object_type_embedding_dim=8,
        )