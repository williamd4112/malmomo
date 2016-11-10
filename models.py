import base_network
import numpy as np
import signal
import tensorflow as tf
import tensorflow.contrib.slim as slim
import util

def add_opts(parser):
  parser.add_argument('--share-conv-net', type=bool, default=True,
                      help="if set (dft) we have one network for processing input img that"
                           " is shared between value, l_value and output_action networks."
                           " if not set each net has it's own network.")
  parser.add_argument('--discount', type=float, default=0.99,
                      help="discount for RHS of bellman equation update")
  parser.add_argument('--action-noise-theta', type=float, default=0.01,
                      help="OrnsteinUhlenbeckNoise theta (rate of change) param for action"
                           " exploration")
  parser.add_argument('--action-noise-sigma', type=float, default=0.05,
                      help="OrnsteinUhlenbeckNoise sigma (magnitude) param for action"
                           " exploration")


VERBOSE_DEBUG = False
def toggle_verbose_debug(signal, frame):
  global VERBOSE_DEBUG
  VERBOSE_DEBUG = not VERBOSE_DEBUG
signal.signal(signal.SIGUSR1, toggle_verbose_debug)

class ValueNetwork(base_network.Network):
  """ Value network component of a NAF network. Created as seperate net because it has a target network."""

  def __init__(self, namespace, input_state, opts): #, hidden_layer_config):
    super(ValueNetwork, self).__init__(namespace)

    self.input_state = input_state

    with tf.variable_scope(namespace):
      # expose self.input_state_representation since it will be the network "shared"
      # by l_value & output_action network when running --share-input-state-representation
      self.conv_net_output = self.conv_net_on(input_state, opts)
      self.hidden_layers = self.hidden_layers_on(self.conv_net_output, [100, 50])
      self.value = slim.fully_connected(scope='fc',
                                        inputs=self.hidden_layers,
                                        num_outputs=1,
                                        weights_regularizer=tf.contrib.layers.l2_regularizer(0.01),
                                        activation_fn=None)  # (batch, 1)

  def value_given(self, state):
    return tf.get_default_session().run(self.value,
                                        feed_dict={self.input_state: state,
                                                   base_network.IS_TRAINING: False})

class NafNetwork(base_network.Network):

  def __init__(self, namespace,
               input_state, input_state_2,
               value_net, target_value_net,
               action_dim, opts):
    super(NafNetwork, self).__init__(namespace)

    # noise to apply to actions during rollouts
    self.exploration_noise = util.OrnsteinUhlenbeckNoise(action_dim,
                                                         opts.action_noise_theta,
                                                         opts.action_noise_sigma)

    # we already have the V networks, created independently because it also
    # has a target network.
    self.value_net = value_net
    self.target_value_net = target_value_net

    # keep placeholders provided and build any others required
    self.input_state = input_state
    self.input_state_2 = input_state_2
    self.input_action = tf.placeholder(shape=[None, action_dim],
                                       dtype=tf.float32, name="input_action")
    self.reward =  tf.placeholder(shape=[None, 1],
                                  dtype=tf.float32, name="reward")
    self.terminal_mask = tf.placeholder(shape=[None, 1],
                                        dtype=tf.float32, name="terminal_mask")

    with tf.variable_scope(namespace):
      # mu (output_action) is also a simple NN mapping input state -> action
      # this is our target op for inference (i.e. value that maximises Q given input_state)
      with tf.variable_scope("output_action"):
        if opts.share_conv_net:
          conv_net_output = value_net.conv_net_output
        else:
          conv_net_output = self.conv_net_on(input_state, opts)
        hidden_layers = self.hidden_layers_on(conv_net_output, [100, 50])
        weights_initializer = tf.random_uniform_initializer(-0.001, 0.001)
        self.output_action = slim.fully_connected(scope='fc',
                                                  inputs=hidden_layers,
                                                  num_outputs=action_dim,
                                                  weights_initializer=weights_initializer,
                                                  weights_regularizer=tf.contrib.layers.l2_regularizer(0.01),
                                                  activation_fn=tf.nn.tanh)  # (batch, action_dim)

      # A (advantage) is a bit more work and has three components...
      # first the u / mu difference. note: to use in a matmul we need
      # to convert this vector into a matrix by adding an "unused"
      # trailing dimension
      u_mu_diff = self.input_action - self.output_action  # (batch, action_dim)
      u_mu_diff = tf.expand_dims(u_mu_diff, -1)           # (batch, action_dim, 1)

      # next we have P = L(x).L(x)_T  where L is the values of lower triangular
      # matrix with diagonals exp'd. yikes!

      # first the L lower triangular values; a network on top of the input state
      num_l_values = (action_dim*(action_dim+1))/2
      with tf.variable_scope("l_values"):
        if opts.share_conv_net:
          conv_net_output = value_net.conv_net_output
        else:
          conv_net_output = self.conv_net_on(input_state, opts)
        hidden_layers = self.hidden_layers_on(conv_net_output, [100, 50])
        l_values = slim.fully_connected(scope='fc',
                                        inputs=hidden_layers,
                                        num_outputs=num_l_values,
                                        weights_regularizer=tf.contrib.layers.l2_regularizer(0.01),
                                        activation_fn=None)

      # we will convert these l_values into a matrix one row at a time.
      rows = []

      self._l_values = l_values

      # each row is made of three components;
      # 1) the lower part of the matrix, i.e. elements to the left of diagonal
      # 2) the single diagonal element (that we exponentiate)
      # 3) the upper part of the matrix; all zeros
      batch_size = tf.shape(l_values)[0]
      row_idx = 0
      for row_idx in xrange(action_dim):
        row_offset_in_l = (row_idx*(row_idx+1))/2
        lower = tf.slice(l_values, begin=(0, row_offset_in_l), size=(-1, row_idx))
        diag  = tf.exp(tf.slice(l_values, begin=(0, row_offset_in_l+row_idx), size=(-1, 1)))
        upper = tf.zeros((batch_size, action_dim - tf.shape(lower)[1] - 1)) # -1 for diag
        rows.append(tf.concat(1, [lower, diag, upper]))
      # full L matrix is these rows packed.
      L = tf.pack(rows, 0)
      # and since leading axis in l was always the batch
      # we need to transpose it back to axis0 again
      L = tf.transpose(L, (1, 0, 2))  # (batch_size, action_dim, action_dim)
      self.check_L = tf.check_numerics(L, "L")

      # P is L.L_T
      L_T = tf.transpose(L, (0, 2, 1))  # TODO: update tf & use batch_matrix_transpose
      P = tf.batch_matmul(L, L_T)  # (batch_size, action_dim, action_dim)

      # can now calculate advantage
      u_mu_diff_T = tf.transpose(u_mu_diff, (0, 2, 1))
      advantage = -0.5 * tf.batch_matmul(u_mu_diff_T, tf.batch_matmul(P, u_mu_diff))  # (batch, 1, 1)
      # and finally we need to reshape off the axis we added to be able to matmul
      self.advantage = tf.reshape(advantage, [-1, 1])  # (batch, 1)

      # Q is value + advantage
      self.q_value = value_net.value + self.advantage

      # target y is reward + discounted target value
      self.target_y = self.reward + (self.terminal_mask * opts.discount * \
                                     target_value_net.value)
      self.target_y = tf.stop_gradient(self.target_y)

      # loss is squared difference that we want to minimise.
      self.loss = tf.reduce_mean(tf.pow(self.q_value - self.target_y, 2))
      with tf.variable_scope("optimiser"):
        # dynamically create optimiser based on opts
        optimiser = util.construct_optimiser(opts)
        # calc gradients
        gradients = optimiser.compute_gradients(self.loss)
        # potentially clip and wrap with debugging tf.Print
        gradients = util.clip_and_debug_gradients(gradients, opts)
        # apply
        self.train_op = optimiser.apply_gradients(gradients)

      # sanity checks (in the dependent order)
      checks = []
      for op, name in [(l_values, 'l_values'), (L,'L'), (self.loss, 'loss')]:
        checks.append(tf.check_numerics(op, name))
      self.check_numerics = tf.group(*checks)

  def action_given(self, state, add_noise):
    # NOTE: noise is added _outside_ tf graph. we do this simply because the noisy output
    # is never used for any part of computation graph required for online training. it's
    # only used during training after being the replay buffer.
    actions = tf.get_default_session().run(self.output_action,
                                           feed_dict={self.input_state: [state],
                                                      base_network.IS_TRAINING: False})

    if add_noise:
      if VERBOSE_DEBUG:
        pre_noise = str(actions)
      actions[0] += self.exploration_noise.sample()
      actions = np.clip(1, -1, actions)  # action output is _always_ (-1, 1)
      if VERBOSE_DEBUG:
        print "TRAIN action_given pre_noise %s post_noise %s" % (pre_noise, actions)
    return map(float, np.squeeze(actions))

  def train(self, batch):
    if VERBOSE_DEBUG:
      print "batch.action"
      print batch.action.T
      print "batch.reward", batch.reward.T
      print "batch.terminal_mask", batch.terminal_mask.T
      values = tf.get_default_session().run([self._l_values, self.value_net.value, self.advantage,
                                             self.target_value_net.value, ],
                                            feed_dict={self.input_state: batch.state_1,
                                                       self.input_action: batch.action,
                                                       self.reward: batch.reward,
                                                       self.terminal_mask: batch.terminal_mask,
                                                       self.input_state_2: batch.state_2,
                                                       base_network.IS_TRAINING: True})
      values = [np.squeeze(v) for v in values]
#      print "_l_values"
#      print values[0].T
      print "value_net.value        ", values[1].T
      print "advantage              ", values[2].T
      print "target_value_net.value ", values[3].T

    _, _, l = tf.get_default_session().run([self.check_numerics, self.train_op, self.loss],
                                           feed_dict={self.input_state: batch.state_1,
                                                      self.input_action: batch.action,
                                                      self.reward: batch.reward,
                                                      self.terminal_mask: batch.terminal_mask,
                                                      self.input_state_2: batch.state_2,
                                                      base_network.IS_TRAINING: True})
    print "loss\t%s" % l
    return l