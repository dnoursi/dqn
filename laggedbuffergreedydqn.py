#!/usr/bin/env python

from __future__ import division, print_function, unicode_literals

# Handle arguments (before slow imports so --help can be fast)
import argparse
parser = argparse.ArgumentParser(
    description="Train a DQN net for Atari games.")

# Important hparams
parser.add_argument("-g", "--game", type=str, default="Pong")
parser.add_argument("-n", "--number-steps", type=int, default=1000000, help="total number of training steps")
parser.add_argument("-e", "--explore-steps", type=int, default=100000, help="total number of explorartion steps")
parser.add_argument("-c", "--copy-steps", type=int, default=4096, help="number of training steps between copies of online DQN to target DQN")
parser.add_argument("-l", "--learn-freq", type=int, default=4, help="number of game steps between each training step")

parser.add_argument("-b", "--benchmark", type=bool, default=True, help="use heuristic benchmarking")
parser.add_argument("-p", "--proportion-lag", type=float, default=.25)
parser.add_argument("--bb-size", type=int, default=10, help= "Size of benchmark/checkpoint buffer")
parser.add_argument("--bb-freshness", type=int, default=6, help= "Max number of uses of a checkpoint")

# Irrelevant hparams
parser.add_argument("-s", "--save-steps", type=int, default=10000, help="number of training steps between saving checkpoints")
parser.add_argument("-r", "--render", action="store_true", default=False, help="render the game during training or testing")
parser.add_argument("-t", "--test", action="store_true", default=False, help="test (no learning and minimal epsilon)")
parser.add_argument("-v", "--verbosity", action="count", default=1, help="increase output verbosity")
parser.add_argument("-j", "--jobid", default="123123", help="SLURM job ID")

args = parser.parse_args()
print("Args:", args)

from collections import deque
import gym
import numpy as np
import os
import tensorflow as tf
import sys
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
#import seaborn as sns
#import gym.envs.atari as atari
#sns.set()

from util import wrap_dqn

env = wrap_dqn(gym.make("{}NoFrameskip-v4".format(args.game)))
#env = atari.AtariEnv(env)
#env = wrap_dqn(atari.AtariEnv("pong", frameskip=1))

def q_network(net, name, reuse=False):
    with tf.variable_scope(name, reuse=reuse) as scope:
        initializer = tf.contrib.layers.variance_scaling_initializer()
        for n_maps, kernel_size, strides, padding, activation in zip(
                [32, 64, 64], [(8,8), (4,4), (3,3)], [4, 2, 1],
                ["SAME"] * 3 , [tf.nn.relu] * 3):
            net = tf.layers.conv2d(net, filters=n_maps, kernel_size=kernel_size, strides=strides,
                padding=padding, activation=activation, kernel_initializer=initializer)
        net = tf.layers.dense(tf.contrib.layers.flatten(net), 256, activation=tf.nn.relu, kernel_initializer=initializer)
        net = tf.layers.dense(net, env.action_space.n, kernel_initializer=initializer)

    trainable_vars = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope.name)
    return net, trainable_vars

# Now for the training operations
learning_rate = 1e-4
training_start = 10000  # start training after 10,000 game steps
discount_rate = 0.99
batch_size = 64

with tf.variable_scope("train"):
    X_state = tf.placeholder(tf.float32, shape=[None, 84, 84, 4])
    X_next_state = tf.placeholder(tf.float32, shape=[None, 84, 84, 4])
    X_action = tf.placeholder(tf.int32, shape=[None])
    X_done = tf.placeholder(tf.float32, shape=[None])
    X_rewards = tf.placeholder(tf.float32, shape=[None])
    online_q_values, online_vars = q_network(X_state, name="q_networks/online")
    target_q_values, target_vars = q_network(X_next_state, name="q_networks/online", reuse=True)
    max_target_q_values = tf.reduce_max(target_q_values, axis=1)
    target = X_rewards + (1. - X_done) * discount_rate * max_target_q_values
    q_value = tf.reduce_sum(online_q_values * tf.one_hot(X_action, env.action_space.n), axis=1)
    error = tf.abs(q_value - tf.stop_gradient(target))
    clipped_error = tf.clip_by_value(error, 0.0, 1.0)
    linear_error = 2 * (error - clipped_error)
    loss = tf.reduce_mean(tf.square(clipped_error) + linear_error)

    global_step = tf.Variable(0, trainable=False, name='global_step')
    optimizer = tf.train.AdamOptimizer(learning_rate)
    training_op = optimizer.minimize(loss, global_step=global_step)

# We need an operation to copy the online DQN to the target DQN
copy_ops = [target_var.assign(online_var)
            for target_var, online_var in zip(target_vars, online_vars)]
copy_online_to_target = tf.group(*copy_ops)

init = tf.global_variables_initializer()
saver = tf.train.Saver()

# Let's implement a simple replay memory
replay_memory = deque([], maxlen=10000)
current_episode_memory = deque([], maxlen=10000)
current_episode_full_state = deque([], maxlen=10000)

assert 0 < args.proportion_lag < 1

benchmark_buffer = []

def sample_memories(batch_size):
    indices = np.random.permutation(len(replay_memory))[:batch_size]
    cols = [[], [], [], [], []] # state, action, reward, next_state, continue
    for idx in indices:
        memory = replay_memory[idx]
        for col, value in zip(cols, memory):
            col.append(value)
    cols = [np.array(col) for col in cols]
    return cols

# And on to the epsilon-greedy policy with decaying epsilon
eps_min = 0.01
eps_max = 1.0 if not args.test else eps_min

def scaled_eps(step):
    return max(eps_min, eps_max - (eps_max-eps_min) * step / args.explore_steps)

def reverse_scaled_eps(step):
    return min(eps_max, eps_min + (eps_max-eps_min) * step / args.explore_steps)

def epsilon_greedy(q_values, step):
    epsilon = scaled_eps(step)
    if np.random.rand() < epsilon:
        return np.random.randint(env.action_space.n) # random action
    else:
        return np.argmax(q_values) # optimal action

done = True # env needs to be reset

# We will keep track of the max Q-Value over time and compute the mean per game
loss_val = np.infty
game_length = 0
total_max_q = 0
mean_max_q = 0.0
returnn = 0.0
returns = []
steps = []
path = os.path.join(args.jobid, "model")
with tf.Session() as sess:
    if os.path.isfile(path + ".index"):
        saver.restore(sess, path)
    else:
        init.run()
        copy_online_to_target.run()
    for step in range(args.number_steps):
        training_iter = global_step.eval()
        if done: # game over, start again
            if args.verbosity > 0:
                print("Step {}/{} ({:.1f})% Training iters {}   "
                      "Loss {:5f}    Mean Max-Q {:5f}   Return: {:5f}".format(
                step, args.number_steps, step * 100 / args.number_steps,
                training_iter, loss_val, mean_max_q, returnn))
                sys.stdout.flush()

            if (not args.benchmark):
                state = env.reset()
            else:
                if np.random.random() < scaled_eps(step/2):
                    state = env.reset()
                    bench_used = False
                else:
                    bench_used = True
                    if args.bb_size != len(benchmark_buffer):
                        state = env.reset()
                    else:
                        bench_restore_idx = np.random.randint(len(benchmark_buffer))
                        rscore, restore_state, restore_cloned, rcount = benchmark_buffer[bench_restore_idx]
                        state = restore_state
                        env.env.unwrapped.restore_full_state(restore_cloned)

                        if rcount > args.bb_freshness:
                            assert False

                        if rcount >= args.bb_freshness:
                            benchmark_buffer.pop(bench_restore_idx)
                        else:
                            benchmark_buffer[bench_restore_idx] = (rscore, restore_state, restore_cloned, rcount+1)

        if args.render:
            env.render()

        # Online DQN evaluates what to do
        q_values = online_q_values.eval(feed_dict={X_state: [state]})
        action = epsilon_greedy(q_values, step)

        # checkpoint old state
        old_cloned_state = env.env.unwrapped.clone_full_state()

        # Online DQN plays
        next_state, reward, done, info = env.step(action)
        returnn += reward

        # Let's memorize what happened
        current_episode_memory.append((state, action, reward, next_state, done))
        current_episode_full_state.append(old_cloned_state)

        state = next_state

        if args.test:
            continue

        # Compute statistics for tracking progress (not shown in the book)
        total_max_q += q_values.max()
        game_length += 1
        if done:
            if args.benchmark:

                # Dont use this again
                if bench_used and returnn < .1 * np.mean(returns):
                    benchmark_buffer.pop(bench_restore_idx)

                idx = int(args.proportion_lag * reverse_scaled_eps(step/2) * len(current_episode_memory))
                bench_replay = current_episode_memory[idx][0]
                bench_state = current_episode_full_state[idx]
                benchmark_buffer.append( (returnn, bench_replay, bench_state, 0) )
                while len(benchmark_buffer) > args.bb_size:
                    min_return = benchmark_buffer[0][0]
                    mr_index = 0
                    for i in range(len(benchmark_buffer)):
                        tupl = benchmark_buffer[i]
                        if tupl[0] < min_return:
                            min_return = tupl[0]
                            mr_index = i
                    benchmark_buffer.pop(i)

            replay_memory.extend(current_episode_memory)
            current_episode_memory.clear()
            current_episode_full_state.clear()

            steps.append(step)
            returns.append(returnn)
            returnn = 0.
            mean_max_q = total_max_q / game_length
            total_max_q = 0.0
            game_length = 0

        if step < training_start or step % args.learn_freq != 0:
            continue # only train after warmup period and at regular intervals

        # Sample memories and train the online DQN
        X_state_val, X_action_val, X_rewards_val, X_next_state_val, X_done_val = sample_memories(batch_size)

        _, loss_val = sess.run([training_op, loss],
        {X_state: X_state_val,
        X_action: X_action_val,
        X_rewards: X_rewards_val,
        X_done: X_done_val,
        X_next_state: X_next_state_val})

        # Regularly copy the online DQN to the target DQN
        if step % args.copy_steps == 0:
            copy_online_to_target.run()

        # And save regularly
        if step % args.save_steps == 0:

            saver.save(sess, path)
            np.save(os.path.join(args.jobid, "{}.npy".format(args.jobid)), np.array((steps, returns)))
