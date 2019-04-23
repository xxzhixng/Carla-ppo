import os
import re
import shutil

import numpy as np
import tensorflow as tf
import tensorflow_probability as tfp

from utils import build_mlp, create_counter_variable, create_mean_metrics_from_dict

class PolicyGraph():
    """
        Manages the policy computation graph
    """

    def __init__(self, input_states, taken_actions, action_space, scope_name,
                 initial_std=0.4, initial_mean_factor=0.1,
                 pi_hidden_sizes=(500, 300), vf_hidden_sizes=(500, 300)):
        """
            input_states [batch_size, width, height, depth]:
                Input images to predict actions for
            taken_actions [batch_size, num_actions]:
                Placeholder of taken actions for training
            action_space (gym.spaces.Box):
                Continous action space of our agent
            scope_name (string):
                Variable scope name for the policy graph
            initial_std (float):
                Initial value of the std used in the gaussian policy
            initial_mean_factor (float):
                Variance scaling factor for the action mean prediction layer
            pi_hidden_sizes (list):
                List of layer sizes used to construct action predicting MLP
            vf_hidden_sizes (list):
                List of layer sizes used to construct value predicting MLP
        """

        num_actions, action_min, action_max = action_space.shape[0], action_space.low, action_space.high

        with tf.variable_scope(scope_name):
            # Policy branch π(a_t | s_t; θ)
            self.pi = build_mlp(input_states, hidden_sizes=pi_hidden_sizes, activation=tf.nn.relu, output_activation=tf.nn.relu)
            self.action_mean = tf.layers.dense(self.pi, num_actions,
                                               activation=tf.nn.tanh,
                                               kernel_initializer=tf.initializers.variance_scaling(scale=initial_mean_factor),
                                               name="action_mean")
            self.action_mean = action_min + ((self.action_mean + 1) / 2) * (action_max - action_min)
            self.action_logstd = tf.Variable(np.full((num_actions), np.log(initial_std), dtype=np.float32), name="action_logstd")

            # Value branch V(s_t; θ)
            if vf_hidden_sizes is None:
                self.vf = self.pi # Share features if None
            else:
                self.vf = build_mlp(input_states, hidden_sizes=vf_hidden_sizes, activation=tf.nn.relu, output_activation=tf.nn.relu)
            self.value = tf.squeeze(tf.layers.dense(self.vf, 1, activation=None, name="value"), axis=-1)
        
            # Create graph for sampling actions
            self.action_normal  = tfp.distributions.Normal(self.action_mean, tf.exp(self.action_logstd), validate_args=True)
            self.sampled_action = tf.squeeze(self.action_normal.sample(1), axis=0)

            # Clip action space to min max
            self.sampled_action = tf.clip_by_value(self.sampled_action, action_min, action_max)
            
            # Get the log probability of taken actions
            # log π(a_t | s_t; θ)
            self.action_log_prob = tf.reduce_sum(self.action_normal.log_prob(taken_actions), axis=-1, keepdims=True)

class PPO():
    """
        Proximal policy gradient model class
    """

    def __init__(self, input_shape, action_space, num_sub_policies,
                 learning_rate=3e-4, lr_decay=0.998, epsilon=0.2,
                 value_scale=0.5, entropy_scale=0.01, initial_std=0.4,
                 model_dir="./"):
        """
            input_shape [3]:
                Shape of input images as a tuple (width, height, depth)
            action_space (gym.spaces.Box):
                Continous action space of our agent
            num_sub_policies (int):
                Number of sub-policies of our agent
            learning_rate (float):
                Initial learning rate
            lr_decay (float):
                Learning rate decay exponent
            epsilon (float):
                PPO clipping parameter
            value_scale (float):
                Value loss scale factor
            entropy_scale (float):
                Entropy loss scale factor
            initial_std (float):
                Initial value of the std used in the gaussian policy
            model_dir (string):
                Directory to output the trained model and log files
        """
        
        self.num_actions = action_space.shape[0]
        self.num_sub_policies = num_sub_policies

        # Create counters
        self.train_step_counter   = create_counter_variable(name="train_step_counter")
        self.predict_step_counter = create_counter_variable(name="predict_step_counter")
        self.episode_counter      = create_counter_variable(name="episode_counter")
        
        # Create placeholders
        self.input_states      = tf.placeholder(shape=(None, *input_shape), dtype=tf.float32, name="input_state_placeholder")
        self.taken_actions     = tf.placeholder(shape=(None, self.num_actions), dtype=tf.float32, name="taken_action_placeholder")
        self.returns           = tf.placeholder(shape=(None,), dtype=tf.float32, name="returns_placeholder")
        self.advantage         = tf.placeholder(shape=(None,), dtype=tf.float32, name="advantage_placeholder")
        self.active_sub_policy = tf.placeholder(shape=(None,), dtype=tf.int32, name="active_sub_policy_placeholder")

        # Gather filtered output of each sub-policy in these lists
        self.policies, prob_ratios, policy_losses, value_losses, entropy_losses = [], [], [], [], []
        values, action_means, action_logstds, sampled_actions = [], [], [], []

        # Create graphs for each sub-policy
        for sub_policy in range(self.num_sub_policies):
            policy, _, prob_ratio, policy_loss, value_loss, entropy_loss = \
                self._create_sub_policy(sub_policy, self.input_states, self.taken_actions, self.advantage, self.returns, epsilon, value_scale, entropy_scale, action_space, initial_std)
            self.policies.append(policy)

            # Filter out only active sub-policy value
            sub_policy_filter = self.active_sub_policy == sub_policy
            policy_losses.append(policy_loss * sub_policy_filter)
            value_losses.append(value_loss * sub_policy_filter)
            entropy_losses.append(entropy_loss * sub_policy_filter)
            prob_ratios.append(prob_ratio * sub_policy_filter)
            values.append(policy.value * sub_policy_filter)
            action_means.append(policy.action_mean * sub_policy_filter)
            action_logstds.append(policy.action_logstd * sub_policy_filter)
            sampled_actions.append(policy.sampled_action * sub_policy_filter)

        # Reduce sum over all sub-policies (where only the active sub-policy will be non-zero due to previous filtering)
        self.policy_loss = tf.reduce_sum(policy_losses, axis=0)
        self.value_loss = tf.reduce_sum(value_losses, axis=0)
        self.entropy_loss = tf.reduce_sum(entropy_losses, axis=0)
        self.prob_ratio = tf.reduce_sum(prob_ratios, axis=0)
        self.value = tf.reduce_sum(values, axis=0)
        self.action_mean = tf.reduce_sum(action_means, axis=0)
        self.action_logstd = tf.reduce_sum(action_logstds, axis=0)
        self.sampled_action = tf.reduce_sum(sampled_actions, axis=0)
        
        # Final loss
        self.loss = -self.policy_loss + self.value_loss - self.entropy_loss
        
        # Policy parameters
        policy_params     = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope="policy_[0-9]+/")
        policy_old_params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope="policy_old_[0-9]+/")
        assert(len(policy_params) == len(policy_old_params))
        for src, dst in zip(policy_params, policy_old_params):
            assert(src.shape == dst.shape)

        # Minimize loss
        self.learning_rate = tf.train.exponential_decay(learning_rate, self.episode_counter.var, 1, lr_decay, staircase=True)
        self.optimizer     = tf.train.AdamOptimizer(learning_rate=self.learning_rate)
        self.train_step    = self.optimizer.minimize(self.loss, var_list=policy_params)

        # Update network parameters
        self.update_op = tf.group([dst.assign(src) for src, dst in zip(policy_params, policy_old_params)])

        # Set up episodic metrics
        metrics = {}
        metrics["train_loss/policy"] = tf.metrics.mean(self.policy_loss)
        metrics["train_loss/value"] = tf.metrics.mean(self.value_loss)
        metrics["train_loss/entropy"] = tf.metrics.mean(self.entropy_loss)
        metrics["train_loss/loss"] = tf.metrics.mean(self.loss)
        for i in range(self.num_actions):
            metrics["train_actor/action_{}/taken_actions".format(i)] = tf.metrics.mean(tf.reduce_mean(self.taken_actions[:, i]))
            metrics["train_actor/action_{}/mean".format(i)] = tf.metrics.mean(tf.reduce_mean(self.action_mean[:, i]))
            metrics["train_actor/action_{}/std".format(i)] = tf.metrics.mean(tf.reduce_mean(tf.exp(self.action_logstd[i])))
        metrics["train/prob_ratio"] = tf.metrics.mean(tf.reduce_mean(self.prob_ratio))
        metrics["train/returns"] = tf.metrics.mean(tf.reduce_mean(self.returns))
        metrics["train/advantage"] = tf.metrics.mean(tf.reduce_mean(self.advantage))
        metrics["train/learning_rate"] = tf.metrics.mean(tf.reduce_mean(self.learning_rate))
        self.episodic_summaries, self.update_metrics_op = create_mean_metrics_from_dict(metrics)

        # Set up stepwise training summaries
        summaries = []
        for i in range(self.num_actions):
            summaries.append(tf.summary.histogram("train_actor_step/action_{}/taken_actions".format(i), self.taken_actions[:, i]))
            summaries.append(tf.summary.histogram("train_actor_step/action_{}/mean".format(i), self.action_mean[:, i]))
            summaries.append(tf.summary.histogram("train_actor_step/action_{}/std".format(i), tf.exp(self.action_logstd[i])))
        summaries.append(tf.summary.histogram("train_step/input_states", self.input_states))
        summaries.append(tf.summary.histogram("train_step/prob_ratio", self.prob_ratio))
        summaries.append(tf.summary.histogram("train_step/active_sub_policy", self.active_sub_policy))
        self.stepwise_summaries = tf.summary.merge(summaries)

        # Set up stepwise prediction summaries
        summaries = []
        for i in range(self.num_actions):
            summaries.append(tf.summary.scalar("predict_actor/action_{}/sampled_action".format(i), self.sampled_action[0, i]))
            summaries.append(tf.summary.scalar("predict_actor/action_{}/mean".format(i), self.action_mean[0, i]))
            summaries.append(tf.summary.scalar("predict_actor/action_{}/std".format(i), tf.exp(self.action_logstd[i])))
            summaries.append(tf.summary.scalar("predict_actor/active_sub_policy", self.active_sub_policy[0]))
        self.stepwise_prediction_summaries = tf.summary.merge(summaries)

        # Set up model saver and dirs
        self.model_dir = model_dir
        self.saver = tf.train.Saver()
        self.checkpoint_dir = "{}/checkpoints/".format(self.model_dir)
        self.log_dir   = "{}/logs/".format(self.model_dir)
        self.video_dir = "{}/videos/".format(self.model_dir)
        self.dirs = [self.checkpoint_dir, self.log_dir, self.video_dir]
        for d in self.dirs: os.makedirs(d, exist_ok=True)

    def _create_sub_policy(self, sub_policy, input_states, taken_actions, advantage, returns, epsilon, value_scale, entropy_scale, action_space, initial_std):
        # Create policy graphs
        policy     = PolicyGraph(input_states, taken_actions, action_space, "policy_{}".format(sub_policy), initial_std=initial_std)
        policy_old = PolicyGraph(input_states, taken_actions, action_space, "policy_old_{}".format(sub_policy), initial_std=initial_std)

        # Calculate ratio:
        # r_t(θ) = exp( log   π(a_t | s_t; θ) - log π(a_t | s_t; θ_old)   )
        # r_t(θ) = exp( log ( π(a_t | s_t; θ) /     π(a_t | s_t; θ_old) ) )
        # r_t(θ) = π(a_t | s_t; θ) / π(a_t | s_t; θ_old)
        prob_ratio = tf.exp(policy.action_log_prob - policy_old.action_log_prob)

        # Policy loss
        adv = tf.expand_dims(advantage, axis=-1)
        policy_loss = tf.reduce_mean(tf.minimum(prob_ratio * adv, tf.clip_by_value(prob_ratio, 1.0 - epsilon, 1.0 + epsilon) * adv))

        # Value loss = mse(V(s_t) - R_t)
        value_loss = tf.reduce_mean(tf.squared_difference(policy.value, returns)) * value_scale
        
        # Entropy loss
        entropy_loss = tf.reduce_mean(tf.reduce_sum(policy.action_normal.entropy(), axis=-1)) * entropy_scale
        
        return policy, policy_old, prob_ratio, policy_loss, value_loss, entropy_loss

    def init_session(self, sess=None, init_logging=True):
        if sess is None:
            self.sess = tf.Session()
            self.sess.run([tf.global_variables_initializer(), tf.local_variables_initializer()])
        else:
            self.sess = sess

        if init_logging:
            self.train_writer = tf.summary.FileWriter(self.log_dir, self.sess.graph)
        
    def save(self):
        model_checkpoint = os.path.join(self.checkpoint_dir, "model.ckpt")
        self.saver.save(self.sess, model_checkpoint, global_step=self.episode_counter.var)
        print("Model checkpoint saved to {}".format(model_checkpoint))

    def load_latest_checkpoint(self):
        model_checkpoint = tf.train.latest_checkpoint(self.checkpoint_dir)
        if model_checkpoint:
            try:
                self.saver.restore(self.sess, model_checkpoint)
                print("Model checkpoint restored from {}".format(model_checkpoint))
                return True
            except:
                return False
        
    def train(self, input_states, taken_actions, returns, advantage, active_sub_policy):
        _, _, summaries, step_idx = \
            self.sess.run([self.train_step, self.update_metrics_op, self.stepwise_summaries, self.train_step_counter.var],
                feed_dict={
                    self.input_states: input_states,
                    self.taken_actions: taken_actions,
                    self.returns: returns,
                    self.advantage: advantage,
                    self.active_sub_policy: active_sub_policy
                }
            )
        self.train_writer.add_summary(summaries, step_idx)
        self.sess.run(self.train_step_counter.inc_op) # Inc step counter
        
    def predict(self, input_states, sub_policy, greedy=False, write_to_summary=False):
        # Extend input axis 0 if no batch dim
        input_states = np.asarray(input_states)
        if len(input_states.shape) != 2:
            input_states = [input_states]
        sub_policy = np.asarray(sub_policy)
        if len(sub_policy.shape) != 2:
            sub_policy = [sub_policy]

        # Predict action
        action = self.action_mean if greedy else self.sampled_action
        sampled_action, value, summaries, step_idx = \
            self.sess.run([action, self.value, self.stepwise_prediction_summaries, self.predict_step_counter.var],
                feed_dict={
                    self.input_states: input_states,
                    self.active_sub_policy: sub_policy
                }
            )

        if write_to_summary:
            self.train_writer.add_summary(summaries, step_idx)
            self.sess.run(self.predict_step_counter.inc_op)

        # Squeeze output if output has one element
        if len(input_states) == 1:
            return sampled_action[0], value[0]
        return sampled_action, value

    def get_episode_idx(self):
        return self.sess.run(self.episode_counter.var)

    def get_train_step_idx(self):
        return self.sess.run(self.train_step_counter.var)

    def get_predict_step_idx(self):
        return self.sess.run(self.predict_step_counter.var)

    def write_value_to_summary(self, summary_name, value, step):
        summary = tf.Summary()
        summary.value.add(tag=summary_name, simple_value=value)
        self.train_writer.add_summary(summary, step)

    def write_dict_to_summary(self, summary_name, params, step):
        summary_op = tf.summary.text(summary_name, tf.stack([tf.convert_to_tensor([k, str(v)]) for k, v in params.items()]))
        self.train_writer.add_summary(self.sess.run(summary_op))

    def write_episodic_summaries(self):
        self.train_writer.add_summary(self.sess.run(self.episodic_summaries), self.get_episode_idx())
        self.sess.run([self.episode_counter.inc_op, tf.local_variables_initializer()])

    def update_old_policy(self):
        self.sess.run(self.update_op)
