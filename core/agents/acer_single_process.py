from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import numpy as np
import random
import time
import math
import torch
from torch.autograd import Variable, grad, backward
import torch.nn.functional as F

from utils.helpers import ACER_Experience
from utils.distributions import sample_poisson
from optims.helpers import adjust_learning_rate
from core.agent_single_process import AgentSingleProcess

class ACERSingleProcess(AgentSingleProcess):
    def __init__(self, master, process_id=0):
        super(ACERSingleProcess, self).__init__(master, process_id)

        # lstm hidden states
        if self.master.enable_lstm:
            self._reset_on_policy_lstm_hidden_vb_episode() # clear up hidden state
            self._reset_on_policy_lstm_hidden_vb_rollout() # detach the previous variable from the computation graph
            self._reset_off_policy_lstm_hidden_vb()        # clear up hidden state, since sampled batches won't be connected from previous batches

        # # NOTE global variable pi
        # if self.master.enable_continuous:
        #     self.pi_vb = Variable(torch.Tensor([math.pi]).type(self.master.dtype))

        self.master.logger.warning("Registered ACER-SingleProcess-Agent #" + str(self.process_id) + " w/ Env (seed:" + str(self.env.seed) + ").")

    # NOTE: to be called at the beginning of each new episode, clear up the hidden state
    def _reset_on_policy_lstm_hidden_vb_episode(self, training=True): # seq_len, batch_size, hidden_dim
        not_training = not training
        if self.master.enable_continuous:
            # self.on_policy_lstm_hidden_vb = (Variable(torch.zeros(2, self.master.hidden_dim).type(self.master.dtype), volatile=not_training),
            #                                  Variable(torch.zeros(2, self.master.hidden_dim).type(self.master.dtype), volatile=not_training))
            pass
        else:
            # for self.model
            self.on_policy_lstm_hidden_vb = (Variable(torch.zeros(1, self.master.hidden_dim).type(self.master.dtype), volatile=not_training),
                                             Variable(torch.zeros(1, self.master.hidden_dim).type(self.master.dtype), volatile=not_training))
            # for self.master.avg_model # NOTE: no grads are needed to compute on this model, so always volatile
            self.on_policy_avg_lstm_hidden_vb = (Variable(torch.zeros(1, self.master.hidden_dim).type(self.master.dtype), volatile=True),
                                                 Variable(torch.zeros(1, self.master.hidden_dim).type(self.master.dtype), volatile=True))

    # NOTE: to be called at the beginning of each rollout, detach the previous variable from the graph
    def _reset_on_policy_lstm_hidden_vb_rollout(self):
        # for self.model
        self.on_policy_lstm_hidden_vb = (Variable(self.on_policy_lstm_hidden_vb[0].data),
                                         Variable(self.on_policy_lstm_hidden_vb[1].data))
        # for self.master.avg_model
        self.on_policy_avg_lstm_hidden_vb = (Variable(self.on_policy_avg_lstm_hidden_vb[0].data),
                                             Variable(self.on_policy_avg_lstm_hidden_vb[1].data))

    # NOTE: to be called before each off-policy learning phase
    # NOTE: keeping it separate so as not to mess up the on_policy_lstm_hidden_vb if the current on-policy episode has not finished after the last rollout
    def _reset_off_policy_lstm_hidden_vb(self, training=True):
        not_training = not training
        if self.master.enable_continuous:
            # self.off_policy_lstm_hidden_vb = (Variable(torch.zeros(self.master.batch_size * 2, self.master.hidden_dim).type(self.master.dtype), volatile=not_training),
            #                                   Variable(torch.zeros(self.master.batch_size * 2, self.master.hidden_dim).type(self.master.dtype), volatile=not_training))
            pass
        else:
            # for self.model
            self.off_policy_lstm_hidden_vb = (Variable(torch.zeros(self.master.batch_size, self.master.hidden_dim).type(self.master.dtype), volatile=not_training),
                                              Variable(torch.zeros(self.master.batch_size, self.master.hidden_dim).type(self.master.dtype), volatile=not_training))
            # for self.master.avg_model # NOTE: no grads are needed to compute on this model, so always volatile
            self.off_policy_avg_lstm_hidden_vb = (Variable(torch.zeros(self.master.batch_size, self.master.hidden_dim).type(self.master.dtype), volatile=True),
                                                  Variable(torch.zeros(self.master.batch_size, self.master.hidden_dim).type(self.master.dtype), volatile=True))

    def _preprocessState(self, state, is_valotile=False):
        if isinstance(state, list):
            state_vb = []
            for i in range(len(state)):
                state_vb.append(Variable(torch.from_numpy(state[i]).unsqueeze(0).type(self.master.dtype), volatile=is_valotile))
        else:
            state_vb = Variable(torch.from_numpy(state).unsqueeze(0).type(self.master.dtype), volatile=is_valotile)
        return state_vb

    def _forward(self, state_vb, on_policy=True):
        if self.master.enable_continuous:
            pass
        else:
            if self.master.enable_lstm:
                if on_policy:   # learn from the current experience
                    p_vb, q_vb, v_vb, self.on_policy_lstm_hidden_vb    = self.model(state_vb, self.on_policy_lstm_hidden_vb)
                    avg_p_vb, _, _, self.on_policy_avg_lstm_hidden_vb  = self.master.avg_model(state_vb, self.on_policy_avg_lstm_hidden_vb)
                    # then we also need to get an action for the next time step
                    if self.training:
                        action = p_vb.multinomial().data[0][0]
                    else:
                        action = p_vb.max(1)[1].data.squeeze().numpy()[0]
                    return action, p_vb, q_vb, v_vb, avg_p_vb
                else:           # learn from the sampled replays
                    p_vb, q_vb, v_vb, self.off_policy_lstm_hidden_vb   = self.model(state_vb, self.off_policy_lstm_hidden_vb)
                    avg_p_vb, _, _, self.off_policy_avg_lstm_hidden_vb = self.master.avg_model(state_vb, self.off_policy_avg_lstm_hidden_vb)
                    # TODO: do we also need to get an action for the off-policy case? I think so, then the p_vb would be a batch
                    return _, p_vb, q_vb, v_vb, avg_p_vb
            else:
                pass

class ACERLearner(ACERSingleProcess):
    def __init__(self, master, process_id=0):
        master.logger.warning("<===================================> ACER-Learner #" + str(process_id) + " {Env & Model & Memory}")
        super(ACERLearner, self).__init__(master, process_id)

        # NOTE: diff from pure on-policy methods like a3c, acer is capable of
        # NOTE: off-policy learning and can make use of replay buffer
        self.memory = self.master.memory_prototype(capacity = self.master.memory_params.memory_size // self.master.num_processes,
                                                   max_episode_length = self.master.early_stop)

        self._reset_rollout()

        self.training = True    # choose actions by polinomial
        self.model.train(self.training)
        # local counters
        self.frame_step   = 0   # local frame step counter
        self.train_step   = 0   # local train step counter
        self.on_policy_train_step   = 0   # local on-policy  train step counter
        self.off_policy_train_step  = 0   # local off-policy train step counter
        # local training stats
        self.p_loss_avg   = 0.  # global policy loss
        self.v_loss_avg   = 0.  # global value loss
        self.loss_avg     = 0.  # global value loss
        self.loss_counter = 0   # storing this many losses
        self._reset_training_loggings()

        # copy local training stats to global every prog_freq
        self.last_prog = time.time()

    def _reset_training_loggings(self):
        self.p_loss_avg   = 0.
        self.v_loss_avg   = 0.
        self.loss_avg     = 0.
        self.loss_counter = 0

    def _reset_rollout(self):       # for storing the experiences collected through one rollout
        self.rollout = ACER_Experience(state0 = [],
                                       action = [],
                                       reward = [],
                                       state1 = [],
                                       terminal1 = [],
                                       policy_vb = [],
                                       q0_vb = [],
                                       value0_vb = [],
                                       detached_avg_policy_vb = [],
                                       detached_old_policy_vb = [])

    def _get_QretT_vb(self, on_policy=True):
        if self.rollout.terminal1[-1]:  # for terminal sT:     Q_ret = 0
            if on_policy:
                QretT_vb = Variable(torch.zeros(1, 1))
            else:
                # TODO: check here again: for off-policy should be batch_size
                QretT_vb = Variable(torch.zeros(self.master.batch_size, 1))
        else:                           # for non-terminal sT: Qret = V(s_i; /theta)
            sT_vb = self._preprocessState(self.rollout.state1[-1], True)        # bootstrap from last state
            if self.master.enable_continuous:
                pass
            else:
                # NOTE: here get the output v instead of q to be as Q_ret
                if self.master.enable_lstm:
                    if on_policy:
                        _, _, QretT_vb, _ = self.model(sT_vb, self.on_policy_lstm_hidden_vb)    # NOTE: only doing inference here
                    else:
                        _, _, QretT_vb, _ = self.model(sT_vb, self.off_policy_lstm_hidden_vb)   # NOTE: only doing inference here
                else:
                    _, _, QretT_vb = self.model(sT_vb)  # NOTE: only doing inference here
            QretT_vb = Variable(QretT_vb.data)

        return QretT_vb

    def _1st_order_trpo(self, detached_policy_loss_vb, policy_vb, detached_policy_vb, detached_avg_policy_vb):
        # KL divergence k = \delta_{\phi_{\theta}} DKL[ \pi(|\phi_{\theta_a}) || \pi{|\phi_{\theta}}]
        kl_div_vb = F.kl_div(detached_policy_vb.log(), detached_avg_policy_vb, size_average=False)
        # NOTE: k & g are wll w.r.t. the network output, which is policy_vb
        # NOTE: gradient from this part does not need to be propagated back into the model
        # NOTE: that's why we are only using detached policies here
        k_vb = grad(outputs=kl_div_vb,               inputs=detached_policy_vb, retain_graph=False, only_inputs=True)[0]
        g_vb = grad(outputs=detached_policy_loss_vb, inputs=detached_policy_vb, retain_graph=False, only_inputs=True)[0]

        kg_dot_vb = torch.mm(k_vb, torch.t(g_vb))
        kk_dot_vb = torch.mm(k_vb, torch.t(k_vb))
        z_star_vb = g_vb - ((kg_dot_vb - self.master.clip_1st_order_trpo) / kk_dot_vb).clamp(min=0) * k_vb

        # NOTE: we still need to backprop the value loss afterwards, so the graph needs to be retained here
        self.model.zero_grad()
        backward(variables=policy_vb, grad_variables=z_star_vb, retain_graph=True)
        # NOTE: must not call zero_grad before backprop value loss

    def _backward(self, on_policy=True):
        # preparation
        rollout_steps = len(self.rollout.reward)
        if self.master.enable_continuous:
            pass
        else:
            detached_policy_vb = [Variable(self.rollout.policy_vb[i].data, requires_grad=True) for i in range(rollout_steps)] # [rollout_steps x batch_size x action_dim]
            action_batch_vb    = Variable(torch.from_numpy(np.array(self.rollout.action)).view(rollout_steps, -1, 1).long())  # [rollout_steps x batch_size x 1]
            if self.master.use_cuda:
                action_batch_vb = action_batch_vb.cuda()
            detached_policy_log_vb = [torch.log(detached_policy_vb[i]) for i in range(rollout_steps)]
            # detached_entropy_vb    = [- (detached_policy_log_vb[i] * detached_policy_vb[i]).sum(1) for i in range(rollout_steps)] # TODO: check if should keepdim
            detached_policy_log_vb = [detached_policy_log_vb[i].gather(1, action_batch_vb[i]) for i in range(rollout_steps) ]
        QretT_vb = self._get_QretT_vb(on_policy)

        # compute loss
        policy_loss_vb = Variable(torch.zeros(1, 1))
        value_loss_vb  = Variable(torch.zeros(1, 1))
        for i in reversed(range(rollout_steps)):
            # 1. importance sampling weights: /rho = /pi(|s_i) / /mu(|s_i)
            if on_policy:   # 1 for on-policy
                rho_vb = Variable(torch.ones(1, self.master.action_dim))
                rho_vb[0,0] = 50#0.5#Variable(torch.ones(1, self.master.action_dim))
            else:
                pass

            # Q_ret = r_i + /gamma * Q_ret
            QretT_vb = self.master.gamma * QretT_vb + self.rollout.reward[i]
            # A = Q_ret - V(s_i; /theta)
            advantage_vb = QretT_vb - self.rollout.value0_vb[i]
            # g = min(c, /rho_a_i) * /delta_theta * log(/pi(a_i|s_i; /theta)) * A
            detached_policy_loss_vb = - (rho_vb.gather(1, action_batch_vb[i]).clamp(max=self.master.clip_trace) * detached_policy_log_vb[i] * advantage_vb.detach()).mean(0)

            if self.master.enable_bias_correction:# and not on_policy:   # NOTE: have to perform bais correction when off-policy
                # g = g + /sum_a [1 - c / /rho_a]_+ /pi(a|s_i; /theta) * /delta_theta * log(/pi(a|s_i; /theta)) * (Q(s_i, a; /theta) - V(s_i; /theta)
                bias_correction_coefficient_vb = (1 - self.master.clip_trace / rho_vb).clamp(min=0) * detached_policy_vb[i]
                detached_policy_loss_vb -= (bias_correction_coefficient_vb * detached_policy_vb[i].log() * (self.rollout.q0_vb[i].detach() - self.rollout.value0_vb[i].detach())).sum(1, keepdim=True).mean(0)

            if self.master.enable_1st_order_trpo:
                # policy update d_/theta = d_/theta + /partical/theta / /partical/theta * z*
                policy_loss_vb += self._1st_order_trpo(detached_policy_loss_vb, self.rollout.policy_vb[i], detached_policy_vb[i], self.rollout.detached_avg_policy_vb[i])

            single_step_policy_loss = -(rho.gather(1, actions[i]).clamp(max=args.trace_max) * log_prob * A.detach()).mean(0)  # Average over batch
            # Off-policy bias correction
            if off_policy:
                # g = g + /sum_a [1 - c / /rho_a]_+ /pi(a|s_i; /theta) * /delta_theta * log(/pi(a|s_i; /theta)) * (Q(s_i, a; theta) - V(s_i; theta)
                bias_weight = (1 - args.trace_max / rho).clamp(min=0) * policies[i]
                single_step_policy_loss -= (bias_weight * policies[i].log() * (Qs[i].detach() - Vs[i].expand_as(Qs[i]).detach())).sum(1).mean(0)
            if args.trust_region:
                # Policy update d_/theta = d_/theta + /partical/theta / /partical/theta * z*
                policy_loss += _trust_region_loss(model, policies[i], average_policies[i], single_step_policy_loss, args.trust_region_threshold)
            else:
                # Policy update d_/theta = d_/theta + partical_/theta / /partical_/theta * g
                policy_loss += single_step_policy_loss
            # Entropy regularisation d_/theta = d_/theta + /beta * /delta H(/pi(s_i; /theta))
            policy_loss -= args.entropy_weight * -(policies[i].log() * policies[i]).sum(1).mean(0)  # Sum over probabilities, average over batch

        # compute loss
        # loss_vb = Variable(torch.zeros(1))
        # # TODO:
        # loss_vb.backward()
        # torch.nn.utils.clip_grad_norm(self.model.parameters(), self.master.clip_grad)
        #
        self._ensure_global_grads()
        self.master.optimizer.step()
        self.train_step += 1
        self.master.train_step.value += 1

        # adjust learning rate if enabled
        if self.master.lr_decay:
            self.master.lr_adjusted.value = max(self.master.lr * (self.master.steps - self.master.train_step.value) / self.master.steps, 1e-32)
            adjust_learning_rate(self.master.optimizer, self.master.lr_adjusted.value)

        # # update master.avg_model
        # for shared_param, shared_average_param in zip(shared_model.parameters(), shared_average_model.parameters()):
        #     shared_average_param = args.trust_region_decay * shared_average_param + (1 - args.trust_region_decay) * shared_param

        # # log training stats
        # self.p_loss_avg   += policy_loss_vb.data.numpy()
        # self.v_loss_avg   += value_loss_vb.data.numpy()
        # self.loss_avg     += loss_vb.data.numpy()
        # self.loss_counter += 1

    # NOTE: get action from current model, execute in env
    # NOTE: then get ACER_Experience to calculate stats for backward
    # NOTE: push them into replay buffer in the format of {s,a,r,s1,t1,p}
    def _on_policy_rollout(self, episode_steps, episode_reward):
        # reset rollout experiences
        self._reset_rollout()

        t_start = self.frame_step
        # continue to rollout only if:
        # 1. not running out of max steps of this current rollout, and
        # 2. not terminal, and
        # 3. not exceeding max steps of this current episode
        # 4. master not exceeding max train steps
        while (self.frame_step - t_start) < self.master.rollout_steps \
              and not self.experience.terminal1 \
              and (self.master.early_stop is None or episode_steps < self.master.early_stop):
            # NOTE: here first store the last frame: experience.state1 as rollout.state0
            self.rollout.state0.append(self.experience.state1)
            # then get the action to take from rollout.state0 (experience.state1)
            if self.master.enable_continuous:
                pass
            else:
                action, p_vb, q_vb, v_vb, avg_p_vb = self._forward(self._preprocessState(self.experience.state1), on_policy=True)
            # then execute action in env to get a new experience.state1 -> rollout.state1
            self.experience = self.env.step(action)
            # push experience into rollout
            self.rollout.action.append(action)
            self.rollout.reward.append(self.experience.reward)
            self.rollout.state1.append(self.experience.state1)
            self.rollout.terminal1.append(self.experience.terminal1)
            self.rollout.policy_vb.append(p_vb)
            self.rollout.q0_vb.append(q_vb)
            self.rollout.value0_vb.append(v_vb)
            self.rollout.detached_avg_policy_vb.append(avg_p_vb.detach()) # NOTE
            # also push into replay buffer if off-policy learning is enabled
            if self.master.replay_ratio > 0:
                if self.rollout.terminal1[-1]:
                    self.memory.append(self.rollout.state0[-1],
                                       None,
                                       None,
                                       None)
                else:
                    self.memory.append(self.rollout.state0[-1],
                                       self.rollout.action[-1],
                                       self.rollout.reward[-1],
                                       self.rollout.policy_vb[-1].detach())  # NOTE: no graphs needed

            episode_steps += 1
            episode_reward += self.experience.reward
            self.frame_step += 1
            self.master.frame_step.value += 1

            # NOTE: we put this condition in the end to make sure this current rollout won't be empty
            if self.master.train_step.value >= self.master.steps:
                break

        return episode_steps, episode_reward

    # NOTE: sample from replay buffer for a bunch of trajectories
    # NOTE: then fake rollout on them to get ACER_Experience to get stats for backward
    def _off_policy_rollout(self):
        # reset rollout experiences
        self._reset_rollout()

        # first sample trajectories
        # then do the normal forward
        # also need to log some training stats here maybe

        return

    def run(self):
        # make sure processes are not completely synced by sleeping a bit
        time.sleep(int(np.random.rand() * (self.process_id + 5)))

        nepisodes = 0
        nepisodes_solved = 0
        episode_steps = None
        episode_reward = None
        should_start_new = True
        while self.master.train_step.value < self.master.steps:
            # NOTE: on-policy learning  # NOTE: procedure same as a3c, outs differ a bit
            # sync in every step
            self._sync_local_with_global()
            self.model.zero_grad()

            # start of a new episode
            if should_start_new:
                episode_steps = 0
                episode_reward = 0.
                # reset on_policy_lstm_hidden_vb for new episode
                if self.master.enable_lstm:
                    # NOTE: clear hidden state at the beginning of each episode
                    self._reset_on_policy_lstm_hidden_vb_episode()
                # Obtain the initial observation by resetting the environment
                self._reset_experience()
                self.experience = self.env.reset()
                assert self.experience.state1 is not None
                # reset flag
                should_start_new = False
            if self.master.enable_lstm:
                # NOTE: detach the previous hidden variable from the graph at the beginning of each rollout
                self._reset_on_policy_lstm_hidden_vb_rollout()
            # Run a rollout for rollout_steps or until terminal
            episode_steps, episode_reward = self._on_policy_rollout(episode_steps, episode_reward)

            if self.experience.terminal1 or \
               self.master.early_stop and episode_steps >= self.master.early_stop:
                nepisodes += 1
                should_start_new = True
                if self.experience.terminal1:
                    nepisodes_solved += 1

            # calculate loss
            self._backward(on_policy=True)  # NOTE: only train_step will increment inside _backward
            self.on_policy_train_step += 1
            self.master.on_policy_train_step.value += 1

            # NOTE: off-policy learning
            # perfrom some off-policy training once got enough experience
            if self.master.replay_ratio > 0 and len(self.memory) >= self.master.replay_start:
                # sample a number of off-policy episodes based on the replay ratio
                for _ in range(sample_poisson(self.master.replay_ratio)):
                    # sync in every step
                    self._sync_local_with_global()  # TODO: don't know if this is necessary here
                    self.model.zero_grad()

                    self._reset_off_policy_lstm_hidden_vb()
                    self._off_policy_rollout()  # fake rollout, just to collect net outs from sampled trajectories
                    # calculate loss
                    self._backward(on_policy=False) # NOTE: only train_step will increment inside _backward
                    self.off_policy_train_step += 1
                    self.master.off_policy_train_step.value += 1

            # copy local training stats to global at prog_freq, and clear up local stats
            if time.time() - self.last_prog >= self.master.prog_freq:
                self.master.p_loss_avg.value   += self.p_loss_avg
                self.master.v_loss_avg.value   += self.v_loss_avg
                self.master.loss_avg.value     += self.loss_avg
                self.master.loss_counter.value += self.loss_counter
                self._reset_training_loggings()
                self.last_prog = time.time()

class ACEREvaluator(ACERSingleProcess):
    def __init__(self, master, process_id=0):
        master.logger.warning("<===================================> ACER-Evaluator {Env & Model}")
        super(ACEREvaluator, self).__init__(master, process_id)

        self.training = False   # choose actions w/ max probability
        self.model.train(self.training)
        self._reset_loggings()

        self.start_time = time.time()
        self.last_eval = time.time()

    def _reset_loggings(self):
        # training stats across all processes
        self.p_loss_avg_log = []
        self.v_loss_avg_log = []
        self.loss_avg_log = []
        # evaluation stats
        self.entropy_avg_log = []
        self.v_avg_log = []
        self.steps_avg_log = []
        self.steps_std_log = []
        self.reward_avg_log = []
        self.reward_std_log = []
        self.nepisodes_log = []
        self.nepisodes_solved_log = []
        self.repisodes_solved_log = []
        # placeholders for windows for online curve plotting
        if self.master.visualize:
            # training stats across all processes
            self.win_p_loss_avg = "win_p_loss_avg"
            self.win_v_loss_avg = "win_v_loss_avg"
            self.win_loss_avg = "win_loss_avg"
            # evaluation stats
            self.win_entropy_avg = "win_entropy_avg"
            self.win_v_avg = "win_v_avg"
            self.win_steps_avg = "win_steps_avg"
            self.win_steps_std = "win_steps_std"
            self.win_reward_avg = "win_reward_avg"
            self.win_reward_std = "win_reward_std"
            self.win_nepisodes = "win_nepisodes"
            self.win_nepisodes_solved = "win_nepisodes_solved"
            self.win_repisodes_solved = "win_repisodes_solved"

    def _eval_model(self):
        self.last_eval = time.time()
        eval_at_train_step = self.master.train_step.value
        eval_at_frame_step = self.master.frame_step.value
        eval_at_on_policy_train_step  = self.master.on_policy_train_step.value
        eval_at_off_policy_train_step = self.master.off_policy_train_step.value
        # first grab the latest global model to do the evaluation
        self._sync_local_with_global()

        # evaluate
        eval_step = 0

        eval_entropy_log = []
        eval_v_log = []
        eval_nepisodes = 0
        eval_nepisodes_solved = 0
        eval_episode_steps = None
        eval_episode_steps_log = []
        eval_episode_reward = None
        eval_episode_reward_log = []
        eval_should_start_new = True
        while eval_step < self.master.eval_steps:
            # TODO:
            eval_step += 1

        # Logging for this evaluation phase
        loss_counter = self.master.loss_counter.value
        p_loss_avg = self.master.p_loss_avg.value / loss_counter if loss_counter > 0 else 0.
        v_loss_avg = self.master.v_loss_avg.value / loss_counter if loss_counter > 0 else 0.
        loss_avg = self.master.loss_avg.value / loss_counter if loss_counter > 0 else 0.
        self.master._reset_training_loggings()
        self.p_loss_avg_log.append([eval_at_train_step, p_loss_avg])
        self.v_loss_avg_log.append([eval_at_train_step, v_loss_avg])
        self.loss_avg_log.append([eval_at_train_step, loss_avg])
        self.entropy_avg_log.append([eval_at_train_step, np.mean(np.asarray(eval_entropy_log))])
        self.v_avg_log.append([eval_at_train_step, np.mean(np.asarray(eval_v_log))])
        self.steps_avg_log.append([eval_at_train_step, np.mean(np.asarray(eval_episode_steps_log))])
        self.steps_std_log.append([eval_at_train_step, np.std(np.asarray(eval_episode_steps_log))]); del eval_episode_steps_log
        self.reward_avg_log.append([eval_at_train_step, np.mean(np.asarray(eval_episode_reward_log))])
        self.reward_std_log.append([eval_at_train_step, np.std(np.asarray(eval_episode_reward_log))]); del eval_episode_reward_log
        self.nepisodes_log.append([eval_at_train_step, eval_nepisodes])
        self.nepisodes_solved_log.append([eval_at_train_step, eval_nepisodes_solved])
        self.repisodes_solved_log.append([eval_at_train_step, (eval_nepisodes_solved/eval_nepisodes) if eval_nepisodes > 0 else 0.])
        # plotting
        if self.master.visualize:
            self.win_p_loss_avg = self.master.vis.scatter(X=np.array(self.p_loss_avg_log), env=self.master.refs, win=self.win_p_loss_avg, opts=dict(title="p_loss_avg"))
            self.win_v_loss_avg = self.master.vis.scatter(X=np.array(self.v_loss_avg_log), env=self.master.refs, win=self.win_v_loss_avg, opts=dict(title="v_loss_avg"))
            self.win_loss_avg = self.master.vis.scatter(X=np.array(self.loss_avg_log), env=self.master.refs, win=self.win_loss_avg, opts=dict(title="loss_avg"))
            self.win_entropy_avg = self.master.vis.scatter(X=np.array(self.entropy_avg_log), env=self.master.refs, win=self.win_entropy_avg, opts=dict(title="entropy_avg"))
            self.win_v_avg = self.master.vis.scatter(X=np.array(self.v_avg_log), env=self.master.refs, win=self.win_v_avg, opts=dict(title="v_avg"))
            self.win_steps_avg = self.master.vis.scatter(X=np.array(self.steps_avg_log), env=self.master.refs, win=self.win_steps_avg, opts=dict(title="steps_avg"))
            # self.win_steps_std = self.master.vis.scatter(X=np.array(self.steps_std_log), env=self.master.refs, win=self.win_steps_std, opts=dict(title="steps_std"))
            self.win_reward_avg = self.master.vis.scatter(X=np.array(self.reward_avg_log), env=self.master.refs, win=self.win_reward_avg, opts=dict(title="reward_avg"))
            # self.win_reward_std = self.master.vis.scatter(X=np.array(self.reward_std_log), env=self.master.refs, win=self.win_reward_std, opts=dict(title="reward_std"))
            self.win_nepisodes = self.master.vis.scatter(X=np.array(self.nepisodes_log), env=self.master.refs, win=self.win_nepisodes, opts=dict(title="nepisodes"))
            self.win_nepisodes_solved = self.master.vis.scatter(X=np.array(self.nepisodes_solved_log), env=self.master.refs, win=self.win_nepisodes_solved, opts=dict(title="nepisodes_solved"))
            self.win_repisodes_solved = self.master.vis.scatter(X=np.array(self.repisodes_solved_log), env=self.master.refs, win=self.win_repisodes_solved, opts=dict(title="repisodes_solved"))
        # logging
        self.master.logger.warning("Reporting       @ Step: " + str(eval_at_train_step) + " | Elapsed Time: " + str(time.time() - self.start_time))
        self.master.logger.warning("Iteration: {}; lr: {}".format(eval_at_train_step, self.master.lr_adjusted.value))
        self.master.logger.warning("Iteration: {}; on_policy_steps: {}".format(eval_at_train_step, eval_at_on_policy_train_step))
        self.master.logger.warning("Iteration: {}; off_policy_steps: {}".format(eval_at_train_step, eval_at_off_policy_train_step))
        self.master.logger.warning("Iteration: {}; p_loss_avg: {}".format(eval_at_train_step, self.p_loss_avg_log[-1][1]))
        self.master.logger.warning("Iteration: {}; v_loss_avg: {}".format(eval_at_train_step, self.v_loss_avg_log[-1][1]))
        self.master.logger.warning("Iteration: {}; loss_avg: {}".format(eval_at_train_step, self.loss_avg_log[-1][1]))
        self.master._reset_training_loggings()
        self.master.logger.warning("Evaluating      @ Step: " + str(eval_at_train_step) + " | (" + str(eval_at_frame_step) + " frames)...")
        self.master.logger.warning("Evaluation        Took: " + str(time.time() - self.last_eval))
        self.master.logger.warning("Iteration: {}; entropy_avg: {}".format(eval_at_train_step, self.entropy_avg_log[-1][1]))
        self.master.logger.warning("Iteration: {}; v_avg: {}".format(eval_at_train_step, self.v_avg_log[-1][1]))
        self.master.logger.warning("Iteration: {}; steps_avg: {}".format(eval_at_train_step, self.steps_avg_log[-1][1]))
        self.master.logger.warning("Iteration: {}; steps_std: {}".format(eval_at_train_step, self.steps_std_log[-1][1]))
        self.master.logger.warning("Iteration: {}; reward_avg: {}".format(eval_at_train_step, self.reward_avg_log[-1][1]))
        self.master.logger.warning("Iteration: {}; reward_std: {}".format(eval_at_train_step, self.reward_std_log[-1][1]))
        self.master.logger.warning("Iteration: {}; nepisodes: {}".format(eval_at_train_step, self.nepisodes_log[-1][1]))
        self.master.logger.warning("Iteration: {}; nepisodes_solved: {}".format(eval_at_train_step, self.nepisodes_solved_log[-1][1]))
        self.master.logger.warning("Iteration: {}; repisodes_solved: {}".format(eval_at_train_step, self.repisodes_solved_log[-1][1]))
        self.last_eval = time.time()

        # save model
        self.master._save_model(eval_at_train_step, self.reward_avg_log[-1][1])

    def run(self):
        while self.master.train_step.value < self.master.steps:
            if time.time() - self.last_eval > self.master.eval_freq:
                self._eval_model()
        # we also do a final evaluation after training is done
        self._eval_model()

class ACERTester(ACERSingleProcess):
    def __init__(self, master, process_id=0):
        master.logger.warning("<===================================> ACER-Tester {Env & Model}")
        super(ACERTester, self).__init__(master, process_id)

        self.training = False   # choose actions w/ max probability
        self.model.train(self.training)
        self._reset_loggings()

        self.start_time = time.time()

    def _reset_loggings(self):
        # testing stats
        self.steps_avg_log = []
        self.steps_std_log = []
        self.reward_avg_log = []
        self.reward_std_log = []
        self.nepisodes_log = []
        self.nepisodes_solved_log = []
        self.repisodes_solved_log = []
        # placeholders for windows for online curve plotting
        if self.master.visualize:
            # evaluation stats
            self.win_steps_avg = "win_steps_avg"
            self.win_steps_std = "win_steps_std"
            self.win_reward_avg = "win_reward_avg"
            self.win_reward_std = "win_reward_std"
            self.win_nepisodes = "win_nepisodes"
            self.win_nepisodes_solved = "win_nepisodes_solved"
            self.win_repisodes_solved = "win_repisodes_solved"

    def run(self):
        test_step = 0
        test_nepisodes = 0
        test_nepisodes_solved = 0
        test_episode_steps = None
        test_episode_steps_log = []
        test_episode_reward = None
        test_episode_reward_log = []
        test_should_start_new = True
        while test_nepisodes < self.master.test_nepisodes:
            # TODO:
            test_nepisodes += 1

        self.steps_avg_log.append([test_nepisodes, np.mean(np.asarray(test_episode_steps_log))])
        self.steps_std_log.append([test_nepisodes, np.std(np.asarray(test_episode_steps_log))]); del test_episode_steps_log
        self.reward_avg_log.append([test_nepisodes, np.mean(np.asarray(test_episode_reward_log))])
        self.reward_std_log.append([test_nepisodes, np.std(np.asarray(test_episode_reward_log))]); del test_episode_reward_log
        self.nepisodes_log.append([test_nepisodes, test_nepisodes])
        self.nepisodes_solved_log.append([test_nepisodes, test_nepisodes_solved])
        self.repisodes_solved_log.append([test_nepisodes, (test_nepisodes_solved/test_nepisodes) if test_nepisodes > 0 else 0.])
        # plotting
        if self.master.visualize:
            self.win_steps_avg = self.master.vis.scatter(X=np.array(self.steps_avg_log), env=self.master.refs, win=self.win_steps_avg, opts=dict(title="steps_avg"))
            # self.win_steps_std = self.master.vis.scatter(X=np.array(self.steps_std_log), env=self.master.refs, win=self.win_steps_std, opts=dict(title="steps_std"))
            self.win_reward_avg = self.master.vis.scatter(X=np.array(self.reward_avg_log), env=self.master.refs, win=self.win_reward_avg, opts=dict(title="reward_avg"))
            # self.win_reward_std = self.master.vis.scatter(X=np.array(self.reward_std_log), env=self.master.refs, win=self.win_reward_std, opts=dict(title="reward_std"))
            self.win_nepisodes = self.master.vis.scatter(X=np.array(self.nepisodes_log), env=self.master.refs, win=self.win_nepisodes, opts=dict(title="nepisodes"))
            self.win_nepisodes_solved = self.master.vis.scatter(X=np.array(self.nepisodes_solved_log), env=self.master.refs, win=self.win_nepisodes_solved, opts=dict(title="nepisodes_solved"))
            self.win_repisodes_solved = self.master.vis.scatter(X=np.array(self.repisodes_solved_log), env=self.master.refs, win=self.win_repisodes_solved, opts=dict(title="repisodes_solved"))
        # logging
        self.master.logger.warning("Testing  Took: " + str(time.time() - self.start_time))
        self.master.logger.warning("Testing: steps_avg: {}".format(self.steps_avg_log[-1][1]))
        self.master.logger.warning("Testing: steps_std: {}".format(self.steps_std_log[-1][1]))
        self.master.logger.warning("Testing: reward_avg: {}".format(self.reward_avg_log[-1][1]))
        self.master.logger.warning("Testing: reward_std: {}".format(self.reward_std_log[-1][1]))
        self.master.logger.warning("Testing: nepisodes: {}".format(self.nepisodes_log[-1][1]))
        self.master.logger.warning("Testing: nepisodes_solved: {}".format(self.nepisodes_solved_log[-1][1]))
        self.master.logger.warning("Testing: repisodes_solved: {}".format(self.repisodes_solved_log[-1][1]))
