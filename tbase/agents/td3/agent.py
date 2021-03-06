# -*- coding:utf-8 -*-
import time

import torch
import torch.nn as nn

from tbase.agents.base.ac_agent import ACAgent
from tbase.common.logger import logger
from tbase.common.torch_utils import clear_memory, soft_update


class Agent(ACAgent):
    def __init__(self, env=None, args=None, noise_clip=0.2, policy_noise=0.2):
        super(Agent, self).__init__(env, args)
        self.noise_clip = noise_clip
        self.policy_freq = 2
        self.policy_noise = policy_noise
        self.criterion = torch.nn.MSELoss()

    def update_params(self, _obs, _action, _rew, _obs_next, _done, n_iter):
        t_start = time.time()
        # --use the date to update the value
        reward = torch.tensor(_rew, device=self.policy.device,
                              dtype=torch.float)
        reward = reward.reshape(-1, 1)
        done = torch.tensor(~_done, device=self.policy.device,
                            dtype=torch.float)
        done = done.reshape(-1, 1)
        action = torch.from_numpy(_action).to(self.policy.device, torch.float)
        action = action.reshape(action.shape[0], -1)
        # obs 只取最后一天数据做为输入
        obs = torch.from_numpy(_obs).permute(1, 0, 2).to(self.policy.device,
                                                         torch.float)
        obs_next = torch.from_numpy(_obs_next).permute(1, 0, 2).to(
            self.policy.device, torch.float)
        noise = (torch.randn_like(action) * self.policy_noise).clamp(
            -self.noise_clip, self.noise_clip)

        target_act_next = (self.target_policy.action(obs_next) + noise).clamp(
            -self.policy.action_low, self.policy.action_high).detach()

        target_q1_next, target_q2_next = self.target_value.forward(
            obs_next, target_act_next)
        target_q_next = torch.min(target_q1_next, target_q2_next)
        target_q = reward + torch.mul(target_q_next, (done * self.args.gamma))

        q1, q2 = self.value.forward(obs, action)
        # bellman equation
        value_loss = self.criterion(q1, target_q) + self.criterion(q2,
                                                                   target_q)
        self.value_opt.zero_grad()
        value_loss.backward()
        self.value_opt.step()

        policy_loss, loss_reg, act_reg = 0, 0, 0
        # Delayed policy updates
        if n_iter % self.policy_freq == 0:
            # update the policy
            action_new, model_out = self.policy.action(obs, with_reg=True)
            # loss_a 表示 value对action的评分负值（-Q值)
            loss_a = torch.mul(-1, torch.mean(self.value.Q1(obs, action_new)))
            loss_reg = torch.mean(torch.pow(model_out, 2))
            act_reg = torch.mean(torch.pow(action_new, 2)) * 5e-1
            policy_loss = loss_reg + loss_a + act_reg

            self.policy_opt.zero_grad()
            policy_loss.backward()
            nn.utils.clip_grad_norm_(self.policy.parameters(),
                                     self.args.max_grad_norm)
            self.policy_opt.step()

            soft_update(self.target_policy, self.policy, self.args.tau)
            soft_update(self.target_value, self.value, self.args.tau)

        used_time = time.time() - t_start
        return value_loss, policy_loss, loss_reg, act_reg, used_time

    def learn(self):
        if self.args.num_env > 1:
            self.policy.share_memory()

        self.warm_up()
        logger.info("learning started")
        i = 0
        current_portfolio = 1.0
        t_start = time.time()
        for i_iter in range(self.args.max_iter_num):
            with open(self.args.progress_bar_path, "w") as progress_file:
                progress_file.write("%d,%d" % (i_iter, self.args.max_iter_num))
            if self.args.num_env == 1:
                obs, act, rew, obs_t, done, avg_reward, e_t, ports = \
                    self.simple_explore()
            else:
                obs, act, rew, obs_t, done, avg_reward, e_t, ports = \
                    self.explore()
            for p in ports:
                i += 1
                self.writer.add_scalar('reward/portfolio', p, i)
                current_portfolio = p
                if current_portfolio > self.best_portfolio:
                    self.best_portfolio = current_portfolio
                    logger.info("iter: %d, new best portfolio: %.3f" % (
                        i_iter + 1, self.best_portfolio))
                    self.save(self.model_dir)

            self.writer.add_scalar('time/explore', e_t, i_iter)
            v_loss, p_loss, p_reg, act_reg, u_t = self.update_params(
                obs, act, rew, obs_t, done, i_iter)
            if i_iter % self.policy_freq == 0:
                self.writer.add_scalar('time/update', u_t, i_iter)
                self.writer.add_scalar('loss/value', v_loss, i_iter)
                self.writer.add_scalar('loss/policy', p_loss, i_iter)
                self.writer.add_scalar('reg/action', act_reg, i_iter)
                self.writer.add_scalar('reg/policy', p_reg, i_iter)
                self.writer.add_scalar('reward/policy',
                                       torch.tensor(avg_reward), i_iter)

            if (i_iter + 1) % self.args.log_interval == 0:
                msg = "total update time: %.1f secs" % (time.time() - t_start)
                msg += ", iter=%d, avg_reward=%.3f" % (i_iter + 1, avg_reward)
                msg += ", current_portfolio: %.3f" % current_portfolio
                logger.info(msg)
            clear_memory()

        logger.info("Final best portfolio: %.3f" % self.best_portfolio)
        self.save_best_portofolio(self.model_dir)
