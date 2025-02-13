from collections import OrderedDict

import numpy as np
import torch
import torch.optim as optim
from torch import nn as nn
import joblib

import bgp.rlkit.torch.pytorch_util as ptu
from bgp.rlkit.core.eval_util import create_stats_ordered_dict
from bgp.rlkit.torch.torch_rl_algorithm import TorchRLAlgorithm
from bgp.rlkit.torch.sac.policies import MakeDeterministic


class SoftActorCritic(TorchRLAlgorithm):

    def __init__(
            self,
            env,
            policy,
            qf,
            vf,

            policy_lr=1e-3,
            qf_lr=1e-3,
            vf_lr=1e-3,
            policy_mean_reg_weight=1e-3,
            policy_std_reg_weight=1e-3,
            policy_pre_activation_weight=0.,
            optimizer_class=optim.Adam,
            weight_decay=0,
            loss_criterion=None,

            train_policy_with_reparameterization=True,
            soft_target_tau=1e-2,
            plotter=None,
            render_eval_paths=False,
            eval_deterministic=True,

            use_automatic_entropy_tuning=True,
            target_entropy=None,
            gradient_max_value=None,
            **kwargs
    ):
        if eval_deterministic:
            eval_policy = MakeDeterministic(policy)
        else:
            eval_policy = policy
        super().__init__(
            env=env,
            exploration_policy=policy,
            eval_policy=eval_policy,
            **kwargs
        )
        self.policy = policy
        self.qf = qf
        self.vf = vf
        self.train_policy_with_reparameterization = (
            train_policy_with_reparameterization
        )
        self.soft_target_tau = soft_target_tau
        self.policy_mean_reg_weight = policy_mean_reg_weight
        self.policy_std_reg_weight = policy_std_reg_weight
        self.policy_pre_activation_weight = policy_pre_activation_weight
        self.plotter = plotter
        self.render_eval_paths = render_eval_paths
        self.use_automatic_entropy_tuning = use_automatic_entropy_tuning
        if self.use_automatic_entropy_tuning:
            if target_entropy:
                self.target_entropy = target_entropy
            else:
                self.target_entropy = -np.prod(self.env.action_space.shape).item()  # heuristic value from Tuomas
            self.log_alpha = ptu.zeros(1, requires_grad=True, torch_device=self.device)
            self.alpha_optimizer = optimizer_class(
                [self.log_alpha],
                lr=policy_lr,
                weight_decay=weight_decay
            )

        self.target_vf = vf.copy()
        if loss_criterion is None:
            self.qf_criterion = nn.MSELoss()
            self.vf_criterion = nn.MSELoss()
        else:
            self.qf_criterion = loss_criterion()
            self.vf_criterion = loss_criterion()
        self.policy_optimizer = optimizer_class(
            self.policy.parameters(),
            lr=policy_lr,
            weight_decay=weight_decay
        )
        self.qf_optimizer = optimizer_class(
            self.qf.parameters(),
            lr=qf_lr,
            weight_decay=weight_decay
        )
        self.vf_optimizer = optimizer_class(
            self.vf.parameters(),
            lr=vf_lr,
            weight_decay=weight_decay
        )
        self.gradient_max_value = gradient_max_value

    def _do_training(self):
        batch = self.get_batch()
        rewards = batch['rewards']
        terminals = batch['terminals']
        obs = batch['observations']
        actions = batch['actions']
        next_obs = batch['next_observations']

        q_pred = self.qf(obs, actions)
        v_pred = self.vf(obs)
        # Make sure policy accounts for squashing functions like tanh correctly!
        policy_outputs = self.policy(
                obs,
                reparameterize=self.train_policy_with_reparameterization,
                return_log_prob=True,
        )
        new_actions, policy_mean, policy_log_std, log_pi = policy_outputs[:4]
        if self.use_automatic_entropy_tuning:
            """
            Alpha Loss
            """
            alpha_loss = -(self.log_alpha * (log_pi + self.target_entropy).detach()).mean()
            self.alpha_optimizer.zero_grad()
            alpha_loss.backward()
            self.alpha_optimizer.step()
            alpha = self.log_alpha.exp()
        else:
            alpha = 1
            alpha_loss = 0

        """
        QF Loss
        """
        target_v_values = self.target_vf(next_obs)
        q_target = rewards + (1. - terminals) * self.discount * target_v_values
        qf_loss = self.qf_criterion(q_pred, q_target.detach())

        self.qf_optimizer.zero_grad()
        qf_loss.backward()
        if self.gradient_max_value is not None:
            nn.utils.clip_grad_value_(self.qf.parameters(), self.gradient_max_value)
        self.qf_optimizer.step()

        """
        VF Loss
        """
        q_new_actions = self.qf(obs, new_actions)
        v_target = q_new_actions - alpha*log_pi
        vf_loss = self.vf_criterion(v_pred, v_target.detach())

        self.vf_optimizer.zero_grad()
        vf_loss.backward()
        if self.gradient_max_value is not None:
            nn.utils.clip_grad_value_(self.vf.parameters(), self.gradient_max_value)
        self.vf_optimizer.step()

        """
        Policy Loss
        """
        if self.train_policy_with_reparameterization:
            policy_loss = (alpha*log_pi - q_new_actions).mean()
        else:
            log_policy_target = q_new_actions - v_pred
            policy_loss = (
                log_pi * (alpha*log_pi - log_policy_target).detach()
            ).mean()
        mean_reg_loss = self.policy_mean_reg_weight * (policy_mean**2).mean()
        std_reg_loss = self.policy_std_reg_weight * (policy_log_std**2).mean()
        pre_tanh_value = policy_outputs[-1]
        pre_activation_reg_loss = self.policy_pre_activation_weight * (
            (pre_tanh_value**2).sum(dim=1).mean()
        )
        policy_reg_loss = mean_reg_loss + std_reg_loss + pre_activation_reg_loss
        policy_loss = policy_loss + policy_reg_loss

        """
        Update networks
        """

        self.policy_optimizer.zero_grad()
        policy_loss.backward()
        if self.gradient_max_value is not None:
            nn.utils.clip_grad_value_(self.policy.parameters(), self.gradient_max_value)
        self.policy_optimizer.step()

        self._update_target_network()

        """
        Save some statistics for eval using just one batch.
        """
        if self.need_to_update_eval_statistics:
            self.need_to_update_eval_statistics = False
            self.eval_statistics['QF Loss'] = np.mean(ptu.get_numpy(qf_loss))
            self.eval_statistics['VF Loss'] = np.mean(ptu.get_numpy(vf_loss))
            self.eval_statistics['Policy Loss'] = np.mean(ptu.get_numpy(
                policy_loss
            ))
            self.eval_statistics.update(create_stats_ordered_dict(
                'Q Predictions',
                ptu.get_numpy(q_pred),
            ))
            self.eval_statistics.update(create_stats_ordered_dict(
                'V Predictions',
                ptu.get_numpy(v_pred),
            ))
            self.eval_statistics.update(create_stats_ordered_dict(
                'Log Pis',
                ptu.get_numpy(log_pi),
            ))
            self.eval_statistics.update(create_stats_ordered_dict(
                'Policy mu',
                ptu.get_numpy(policy_mean),
            ))
            self.eval_statistics.update(create_stats_ordered_dict(
                'Policy log std',
                ptu.get_numpy(policy_log_std),
            ))
            qf_grads = torch.tensor([], device=self.qf.device)
            for param in self.qf.parameters():
                try:
                    qf_grads = torch.cat((qf_grads, torch.abs(param.grad.data.flatten())))
                except:
                    pass  # seems to be a weird error on mld5 around layernorm
            self.eval_statistics['QF Gradient'] = qf_grads.mean().item()
            vf_grads = torch.tensor([], device=self.vf.device)
            for param in self.vf.parameters():
                try:
                    vf_grads = torch.cat((vf_grads, torch.abs(param.grad.data.flatten())))
                except:
                    pass  # seems to be a weird error on mld5 around layernorm
            self.eval_statistics['VF Gradient'] = vf_grads.mean().item()
            policy_grads = torch.tensor([], device=self.policy.device)
            for param in self.policy.parameters():
                try:
                    policy_grads = torch.cat((policy_grads, torch.abs(param.grad.data.flatten())))
                except:
                    pass  # seems to be a weird error on mld5 around layernorm
            self.eval_statistics['Policy Gradient'] = policy_grads.mean().item()
            grads = torch.cat((qf_grads, vf_grads, policy_grads))
            self.eval_statistics['Gradient'] = grads.mean().item()
            if self.use_automatic_entropy_tuning:
                self.eval_statistics['Alpha'] = alpha.item()
                self.eval_statistics['Alpha Loss'] = alpha_loss.item()

    @property
    def networks(self):
        return [
            self.policy,
            self.qf,
            self.vf,
            self.target_vf,
        ]

    def _update_target_network(self):
        ptu.soft_update_from_to(self.vf, self.target_vf, self.soft_target_tau)

    def get_epoch_snapshot(self, epoch):
        snapshot = super().get_epoch_snapshot(epoch)
        snapshot.update(
            qf=self.qf,
            policy=self.policy,
            vf=self.vf,
            target_vf=self.target_vf,
            qf_optim=self.qf_optimizer,
            vf_optim=self.vf_optimizer,
            policy_optim=self.policy_optimizer,
        )
        if self.use_automatic_entropy_tuning:
            snapshot.update(alpha=self.log_alpha,
                            alpha_optim=self.alpha_optimizer)
        return snapshot
