"""Baseline models for use in the object navigation task.

Object navigation is currently available as a Task in AI2-THOR and
Facebook's Habitat.
"""
import typing
from typing import cast, Tuple, Dict, Optional

import gym
import torch
import torch.nn as nn
from gym.spaces.dict import Dict as SpaceDict

from core.models.basic_models import SimpleCNN, RNNStateEncoder
from core.algorithms.onpolicy_sync.policy import (
    ActorCriticModel,
    LinearCriticHead,
    LinearActorHead,
    DistributionType,
    Memory,
    ObservationType,
)
from core.base_abstractions.misc import ActorCriticOutput
from core.base_abstractions.distributions import CategoricalDistr


class ObjectManipBaselineActorCritic(ActorCriticModel[CategoricalDistr]):
    """Baseline recurrent actor critic model for object-navigation.

    # Attributes
    action_space : The space of actions available to the agent. Currently only discrete
        actions are allowed (so this space will always be of type `gym.spaces.Discrete`).
    observation_space : The observation space expected by the agent. This observation space
        should include (optionally) 'rgb' images and 'depth' images and is required to
        have a component corresponding to the goal `goal_sensor_uuid`.
    goal_sensor_uuid : The uuid of the sensor of the goal object. See `GoalObjectTypeThorSensor`
        as an example of such a sensor.
    hidden_size : The hidden size of the GRU RNN.
    object_type_embedding_dim: The dimensionality of the embedding corresponding to the goal
        object type.
    """

    def __init__(
        self,
        action_space: gym.spaces.Discrete,
        observation_space: SpaceDict,
        goal_action_uuid: str,
        goal_object_uuid: str,
        arm_collision_uuid: str,
        arm_state_uuid: str,
        hidden_size=512,
        object_type_embedding_dim=8,
        action_type_embedding_dim=8,
        arm_collision_embedding_dim=8,
        arm_state_embedding_dim=64,
        trainable_masked_hidden_state: bool = False,
        num_rnn_layers=1,
        rnn_type="GRU",
    ):
        """Initializer.

        See class documentation for parameter definitions.
        """
        super().__init__(action_space=action_space, observation_space=observation_space)

        self.arm_state_uuid = arm_state_uuid
        self.goal_action_uuid = goal_action_uuid
        self.goal_object_uuid = goal_object_uuid
        self.arm_collision_uuid = arm_collision_uuid

        self._n_object_types = self.observation_space.spaces[self.goal_object_uuid].n
        self._n_action_types = self.observation_space.spaces[self.goal_action_uuid].n
        self._n_collision_state = self.observation_space.spaces[self.arm_collision_uuid].n
        self._n_arm_state = self.observation_space.spaces[self.arm_state_uuid].shape[0]

        self._hidden_size = hidden_size
        self.object_type_embedding_size = object_type_embedding_dim
        self.action_type_embedding_size = action_type_embedding_dim

        self.visual_encoder = SimpleCNN(self.observation_space, self._hidden_size)
        
        self.state_encoder = RNNStateEncoder(
            (0 if self.is_blind else self._hidden_size) + action_type_embedding_dim + object_type_embedding_dim + arm_collision_embedding_dim + arm_state_embedding_dim,
            self._hidden_size,
            trainable_masked_hidden_state=trainable_masked_hidden_state,
            num_layers=num_rnn_layers,
            rnn_type=rnn_type,
        )

        self.actor = LinearActorHead(self._hidden_size, action_space.n)
        self.critic = LinearCriticHead(self._hidden_size)

        self.object_type_embedding = nn.Embedding(
            num_embeddings=self._n_object_types,
            embedding_dim=object_type_embedding_dim,
        )

        self.action_type_embedding = nn.Embedding(
            num_embeddings=self._n_action_types,
            embedding_dim=action_type_embedding_dim,
        )
        self.arm_state_embedding = nn.Linear(self._n_arm_state, arm_state_embedding_dim)

        self.arm_collision_embedding = nn.Embedding(
            num_embeddings=self._n_collision_state,
            embedding_dim=arm_collision_embedding_dim,  
        )

        self.train()

    @property
    def recurrent_hidden_state_size(self) -> int:
        """The recurrent hidden state size of the model."""
        return self._hidden_size

    @property
    def is_blind(self) -> bool:
        """True if the model is blind (e.g. neither 'depth' or 'rgb' is an
        input observation type)."""
        return self.visual_encoder.is_blind

    @property
    def num_recurrent_layers(self) -> int:
        """Number of recurrent hidden layers."""
        return self.state_encoder.num_recurrent_layers

    def _recurrent_memory_specification(self):
        return dict(
            rnn=(
                (
                    ("layer", self.num_recurrent_layers),
                    ("sampler", None),
                    ("hidden", self.recurrent_hidden_state_size),
                ),
                torch.float32,
            )
        )

    def get_object_type_encoding(
        self, observations: Dict[str, torch.FloatTensor]
    ) -> torch.FloatTensor:
        """Get the object type encoding from input batched observations."""
        return self.object_type_embedding(  # type:ignore
            observations[self.goal_object_uuid].to(torch.int64)
        )

    def get_action_type_encoding(
        self,  observations: Dict[str, torch.FloatTensor]
    ) -> torch.FloatTensor:
        return self.action_type_embedding(  # type:ignore
            observations[self.goal_action_uuid].to(torch.int64)
        )        

    def get_arm_state_encoding(
        self, observations: Dict[str, torch.FloatTensor]
    ) -> torch.FloatTensor:
        return self.arm_state_embedding(
            observations[self.arm_state_uuid].to(torch.float)
        )

    def get_arm_collision_encoding(
        self, observations: Dict[str, torch.FloatTensor]
    ) -> torch.FloatTensor:
        return self.arm_collision_embedding(
            observations[self.arm_collision_uuid].to(torch.int64)
        )

    def forward(  # type: ignore
        self,
        observations: Dict[str, torch.FloatTensor],
        memory: Memory,
        prev_actions: torch.LongTensor,
        masks: torch.FloatTensor,
    ) -> Tuple[ActorCriticOutput, torch.FloatTensor]:
        """Processes input batched observations to produce new actor and critic
        values. Processes input batched observations (along with prior hidden
        states, previous actions, and masks denoting which recurrent hidden
        states should be masked) and returns an `ActorCriticOutput` object
        containing the model's policy (distribution over actions) and
        evaluation of the current state (value).

        # Parameters
        observations : Batched input observations.
        rnn_hidden_states : Hidden states from initial timepoints.
        prev_actions : Tensor of previous actions taken.
        masks : Masks applied to hidden states. See `RNNStateEncoder`.
        # Returns
        Tuple of the `ActorCriticOutput` and recurrent hidden state.
        """
        target_object_encoding = self.get_object_type_encoding(observations)
        target_action_encoding = self.get_action_type_encoding(observations)
        arm_collision_encoding = self.get_arm_collision_encoding(observations)
        arm_state_encoding = self.get_arm_state_encoding(observations)
        x = [target_object_encoding, target_action_encoding, arm_collision_encoding, arm_state_encoding]

        if not self.is_blind:
            perception_embed = self.visual_encoder(observations)
            x = [perception_embed] + x

        x_cat = torch.cat(x, dim=2)  # type: ignore
        x_out, rnn_hidden_states = self.state_encoder(
            x_cat, memory.tensor("rnn"), masks
        )

        return (
            ActorCriticOutput(
                distributions=self.actor(x_out), values=self.critic(x_out), extras={}
            ),
            memory.set_tensor("rnn", rnn_hidden_states),
        )

class ResnetTensorObjectManipActorCritic(ActorCriticModel[CategoricalDistr]):
    def __init__(
        self,
        action_space: gym.spaces.Discrete,
        observation_space: SpaceDict,
        goal_sensor_uuid: str,
        rgb_resnet_preprocessor_uuid: Optional[str],
        arm_collision_uuid: str,
        arm_state_uuid: str,
        depth_resnet_preprocessor_uuid: Optional[str] = None,
        hidden_size: int = 512,
        goal_dims: int = 32,
        resnet_compressor_hidden_out_dims: Tuple[int, int] = (128, 32),
        combiner_hidden_out_dims: Tuple[int, int] = (128, 32),
        arm_collision_embedding_dim=32,
        arm_state_embedding_dim=32,
    ):

        super().__init__(
            action_space=action_space, observation_space=observation_space,
        )

        self._hidden_size = hidden_size
        if (
            rgb_resnet_preprocessor_uuid is None
            or depth_resnet_preprocessor_uuid is None
        ):
            resnet_preprocessor_uuid = (
                rgb_resnet_preprocessor_uuid
                if rgb_resnet_preprocessor_uuid is None
                else depth_resnet_preprocessor_uuid
            )
            self.goal_visual_encoder = ResnetTensorGoalEncoder(
                self.observation_space,
                goal_sensor_uuid,
                rgb_resnet_preprocessor_uuid,
                arm_collision_uuid,
                arm_state_uuid,
                goal_dims,
                resnet_compressor_hidden_out_dims,
                combiner_hidden_out_dims,
                arm_collision_embedding_dim,
                arm_state_embedding_dim,
            )
        else:
            self.goal_visual_encoder = ResnetDualTensorGoalEncoder(  # type:ignore
                self.observation_space,
                goal_sensor_uuid,
                rgb_resnet_preprocessor_uuid,
                depth_resnet_preprocessor_uuid,
                goal_dims,
                resnet_compressor_hidden_out_dims,
                combiner_hidden_out_dims,
            )
        self.state_encoder = RNNStateEncoder(
            self.goal_visual_encoder.output_dims, self._hidden_size,
        )
        self.actor = LinearActorHead(self._hidden_size, action_space.n)
        self.critic = LinearCriticHead(self._hidden_size)
        self.train()

    @property
    def recurrent_hidden_state_size(self) -> int:
        """The recurrent hidden state size of the model."""
        return self._hidden_size

    @property
    def is_blind(self) -> bool:
        """True if the model is blind (e.g. neither 'depth' or 'rgb' is an
        input observation type)."""
        return self.goal_visual_encoder.is_blind

    @property
    def num_recurrent_layers(self) -> int:
        """Number of recurrent hidden layers."""
        return self.state_encoder.num_recurrent_layers

    def _recurrent_memory_specification(self):
        return dict(
            rnn=(
                (
                    ("layer", self.num_recurrent_layers),
                    ("sampler", None),
                    ("hidden", self.recurrent_hidden_state_size),
                ),
                torch.float32,
            )
        )
        
    def get_object_type_encoding(
        self, observations: Dict[str, torch.FloatTensor]
    ) -> torch.FloatTensor:
        """Get the object type encoding from input batched observations."""
        return self.goal_visual_encoder.get_object_type_encoding(observations)

    def forward(self, observations, memory, prev_actions, masks):
        x = self.goal_visual_encoder(observations)
        x, rnn_hidden_states = self.state_encoder(x,  memory.tensor("rnn"), masks)
        return (
            ActorCriticOutput(
                distributions=self.actor(x), values=self.critic(x), extras={}
            ),
            memory.set_tensor("rnn", rnn_hidden_states),
        )

class ResnetTensorGoalEncoder(nn.Module):
    def __init__(
        self,
        observation_spaces: SpaceDict,
        goal_sensor_uuid: str,
        rgb_resnet_preprocessor_uuid: str,
        arm_collision_uuid: str,
        arm_state_uuid: str,
        class_dims: int = 32,
        resnet_compressor_hidden_out_dims: Tuple[int, int] = (128, 32),
        combiner_hidden_out_dims: Tuple[int, int] = (128, 32),
        arm_collision_embedding_dim=32,
        arm_state_embedding_dim=32,
    ) -> None:
        super().__init__()
        self.goal_uuid = goal_sensor_uuid
        self.resnet_uuid = rgb_resnet_preprocessor_uuid
        self.arm_collision_uuid = arm_collision_uuid
        self.arm_state_uuid = arm_state_uuid
        self.class_dims = class_dims
        self.arm_collision_embedding_dim = arm_collision_embedding_dim
        self.arm_state_embedding_dim = arm_state_embedding_dim
        self.resnet_hid_out_dims = resnet_compressor_hidden_out_dims
        self.combine_hid_out_dims = combiner_hidden_out_dims

        self.embed_class = nn.Embedding(
            num_embeddings=observation_spaces.spaces[self.goal_uuid].n,
            embedding_dim=self.class_dims,
        )
        self.arm_state_embedding = nn.Linear(
            observation_spaces.spaces[arm_state_uuid].shape[0], 
            arm_state_embedding_dim)

        self.arm_collision_embedding = nn.Embedding(
            num_embeddings=observation_spaces.spaces[arm_collision_uuid].n,
            embedding_dim=arm_collision_embedding_dim,  
        )

        self.blind = self.resnet_uuid not in observation_spaces.spaces
        if not self.blind:
            self.resnet_tensor_shape = observation_spaces.spaces[self.resnet_uuid].shape
            self.resnet_compressor = nn.Sequential(
                nn.Conv2d(self.resnet_tensor_shape[0], self.resnet_hid_out_dims[0], 1),
                nn.ReLU(),
                nn.Conv2d(*self.resnet_hid_out_dims[0:2], 1),
                nn.ReLU(),
            )
            self.target_obs_combiner = nn.Sequential(
                nn.Conv2d(
                    self.resnet_hid_out_dims[1] + self.class_dims + self.arm_collision_embedding_dim + self.arm_state_embedding_dim,
                    self.combine_hid_out_dims[0],
                    1,
                ),
                nn.ReLU(),
                nn.Conv2d(*self.combine_hid_out_dims[0:2], 1),
            )

    @property
    def is_blind(self):
        return self.blind

    @property
    def output_dims(self):
        if self.blind:
            return self.class_dims
        else:
            return (
                self.combine_hid_out_dims[-1]
                * self.resnet_tensor_shape[1]
                * self.resnet_tensor_shape[2]
            )

    def get_object_type_encoding(
        self, observations: Dict[str, torch.FloatTensor]
    ) -> torch.FloatTensor:
        """Get the object type encoding from input batched observations."""
        return typing.cast(
            torch.FloatTensor,
            self.embed_class(observations[self.goal_uuid].to(torch.int64)),
        )

    def compress_resnet(self, observations):
        return self.resnet_compressor(observations[self.resnet_uuid])

    def distribute_target(self, observations):
        target_emb = self.embed_class(observations[self.goal_uuid])
        return target_emb.view(-1, self.class_dims, 1, 1).expand(
            -1, -1, self.resnet_tensor_shape[-2], self.resnet_tensor_shape[-1]
        )

    def arm_state_embed(self, observations):
        state_emb = self.arm_state_embedding(observations[self.arm_state_uuid])
        return state_emb.view(-1, self.arm_state_embedding_dim, 1, 1).expand(
            -1, -1, self.resnet_tensor_shape[-2], self.resnet_tensor_shape[-1]
        )

    def arm_collision_embed(self, observations):
        collision_emb = self.arm_collision_embedding(observations[self.arm_collision_uuid])
        return collision_emb.view(-1, self.arm_collision_embedding_dim, 1, 1).expand(
            -1, -1, self.resnet_tensor_shape[-2], self.resnet_tensor_shape[-1]
        )
        
    def forward(self, observations):
        if self.blind:
            return self.embed_class(observations[self.goal_uuid])
        embs = [
            self.compress_resnet(observations),
            self.distribute_target(observations),
            self.arm_state_embed(observations),
            self.arm_collision_embed(observations),
        ]
        x = self.target_obs_combiner(torch.cat(embs, dim=1,))
        return x.view(x.size(0), -1)  # flatten


class ResnetDualTensorGoalEncoder(nn.Module):
    def __init__(
        self,
        observation_spaces: SpaceDict,
        goal_sensor_uuid: str,
        rgb_resnet_preprocessor_uuid: str,
        depth_resnet_preprocessor_uuid: str,
        class_dims: int = 32,
        resnet_compressor_hidden_out_dims: Tuple[int, int] = (128, 32),
        combiner_hidden_out_dims: Tuple[int, int] = (128, 32),
    ) -> None:
        super().__init__()
        self.goal_uuid = goal_sensor_uuid
        self.rgb_resnet_uuid = rgb_resnet_preprocessor_uuid
        self.depth_resnet_uuid = depth_resnet_preprocessor_uuid
        self.class_dims = class_dims
        self.resnet_hid_out_dims = resnet_compressor_hidden_out_dims
        self.combine_hid_out_dims = combiner_hidden_out_dims
        self.embed_class = nn.Embedding(
            num_embeddings=observation_spaces.spaces[self.goal_uuid].n,
            embedding_dim=self.class_dims,
        )
        self.blind = (
            self.rgb_resnet_uuid not in observation_spaces.spaces
            or self.depth_resnet_uuid not in observation_spaces.spaces
        )
        if not self.blind:
            self.resnet_tensor_shape = observation_spaces.spaces[
                self.rgb_resnet_uuid
            ].shape
            self.rgb_resnet_compressor = nn.Sequential(
                nn.Conv2d(self.resnet_tensor_shape[0], self.resnet_hid_out_dims[0], 1),
                nn.ReLU(),
                nn.Conv2d(*self.resnet_hid_out_dims[0:2], 1),
                nn.ReLU(),
            )
            self.depth_resnet_compressor = nn.Sequential(
                nn.Conv2d(self.resnet_tensor_shape[0], self.resnet_hid_out_dims[0], 1),
                nn.ReLU(),
                nn.Conv2d(*self.resnet_hid_out_dims[0:2], 1),
                nn.ReLU(),
            )
            self.rgb_target_obs_combiner = nn.Sequential(
                nn.Conv2d(
                    self.resnet_hid_out_dims[1] + self.class_dims,
                    self.combine_hid_out_dims[0],
                    1,
                ),
                nn.ReLU(),
                nn.Conv2d(*self.combine_hid_out_dims[0:2], 1),
            )
            self.depth_target_obs_combiner = nn.Sequential(
                nn.Conv2d(
                    self.resnet_hid_out_dims[1] + self.class_dims,
                    self.combine_hid_out_dims[0],
                    1,
                ),
                nn.ReLU(),
                nn.Conv2d(*self.combine_hid_out_dims[0:2], 1),
            )

    @property
    def is_blind(self):
        return self.blind

    @property
    def output_dims(self):
        if self.blind:
            return self.class_dims
        else:
            return (
                2
                * self.combine_hid_out_dims[-1]
                * self.resnet_tensor_shape[1]
                * self.resnet_tensor_shape[2]
            )

    def get_object_type_encoding(
        self, observations: Dict[str, torch.FloatTensor]
    ) -> torch.FloatTensor:
        """Get the object type encoding from input batched observations."""
        return typing.cast(
            torch.FloatTensor,
            self.embed_class(observations[self.goal_uuid].to(torch.int64)),
        )

    def compress_rgb_resnet(self, observations):
        return self.rgb_resnet_compressor(observations[self.rgb_resnet_uuid])

    def compress_depth_resnet(self, observations):
        return self.depth_resnet_compressor(observations[self.depth_resnet_uuid])

    def distribute_target(self, observations):
        target_emb = self.embed_class(observations[self.goal_uuid])
        return target_emb.view(-1, self.class_dims, 1, 1).expand(
            -1, -1, self.resnet_tensor_shape[-2], self.resnet_tensor_shape[-1]
        )

    def forward(self, observations):
        if self.blind:
            return self.embed_class(observations[self.goal_uuid])
        rgb_embs = [
            self.compress_rgb_resnet(observations),
            self.distribute_target(observations),
        ]
        rgb_x = self.rgb_target_obs_combiner(torch.cat(rgb_embs, dim=1,))
        depth_embs = [
            self.compress_depth_resnet(observations),
            self.distribute_target(observations),
        ]
        depth_x = self.depth_target_obs_combiner(torch.cat(depth_embs, dim=1,))
        x = torch.cat([rgb_x, depth_x], dim=1)
        return x.view(x.size(0), -1)  # flatten