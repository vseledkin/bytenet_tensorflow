import tensorflow as tf

from bytenet_ops import causal_conv, mu_law_encode
import bytenet_ops


def create_variable(name, shape):
    '''Create a convolution filter variable with the specified name and shape,
    and initialize it using Xavier initialition.'''
    initializer = tf.contrib.layers.xavier_initializer_conv2d()
    variable = tf.Variable(initializer(shape=shape), name=name)
    return variable


def create_bias_variable(name, shape):
    '''Create a bias variable with the specified name and shape and initialize
    it to zero.'''
    initializer = tf.constant_initializer(value=0.0, dtype=tf.float32)
    return tf.Variable(initializer(shape=shape), name)


class ByteNetModel(object):
    '''Implements the WaveNet network for generative audio.

    Usage (with the architecture as in the DeepMind paper):
        dilations = [2**i for i in range(N)] * M
        filter_width = 2  # Convolutions just use 2 samples.
        residual_channels = 16  # Not specified in the paper.
        dilation_channels = 32  # Not specified in the paper.
        skip_channels = 16      # Not specified in the paper.

        source_network = bytenet_model.ByteNetModel(args)
        source_output = source_network.create_source_network(inputs)

        target_network = bytenet_model.ByteNetModel(args) 
        output = target_network.create_target_network(source_output, conditional_inputs) #this has not been implemented


        in bytenet paper, they have 25 residual blocks with five sets of five rates:

        [1, 2, 4, 8, 16] * 5

        In the wavenet paper, it appeared that they had rates that were far larger -- up to 512 in rates. May be something to test out down the road.
    '''

    def __init__(self,
                 batch_size,
                 dilations=[2**i for i in xrange(5)] * 5 , #investigate this configuration nick
                 filter_width=1, #bytenet calls for a filter width of 1
                 residual_channels=1024,
                 dilation_channels = 1024, # I believe this would be d as they report in the paper
                 skip_channels = 1024,
                 quantization_channels=2**8,
                 use_biases=False,
                 scalar_input=True, # For text this should be marked as True (embedding input)
                 initial_filter_width=2, #nick this was 32 but reduced for memory purposes
                 histograms=False, 
                 initial_channels = 1,
                 FLAGS = None):
        '''Initializes the ByteNet model.

        Args:
            batch_size: How many audio files are supplied per batch
                (recommended: 1).
            dilations: A list with the dilation factor for each layer.
            filter_width: The samples that are included in each convolution,
                after dilating.
            residual_channels: How many filters to learn for the residual.
            dilation_channels: How many filters to learn for the dilated
                convolution.
            skip_channels: How many filters to learn that contribute to the
                quantized softmax output.
            quantization_channels: How many amplitude values to use for audio
                quantization and the corresponding one-hot encoding.
                Default: 256 (8-bit quantization).
            use_biases: Whether to add a bias layer to each convolution.
                Default: False.
            scalar_input: Whether to use the quantized waveform directly as
                input to the network instead of one-hot encoding it.
                Default: False.
            initial_filter_width: The width of the initial filter of the
                convolution applied to the scalar input. This is only relevant
                if scalar_input=True.
            initial_channels: Size of the inputs (normally embedding size)
            histograms: Whether to store histograms in the summary.
                Default: False.
        '''
        self.batch_size = batch_size
        self.dilations = dilations
        self.filter_width = filter_width
        self.residual_channels = residual_channels
        self.dilation_channels = dilation_channels
        self.quantization_channels = quantization_channels
        self.use_biases = use_biases
        self.skip_channels = skip_channels
        self.scalar_input = scalar_input
        self.initial_filter_width = initial_filter_width
        self.histograms = histograms
        self.initial_channels = initial_channels
        self.FLAGS = FLAGS

        self._log_var_stats()
        self.variables = self._create_variables()

    def _log_var_stats(self):
        tf.logging.info('initial channels are: '+str(self.initial_channels))
        tf.logging.info('dilation channels are: '+str(self.dilation_channels))
        tf.logging.info('quantization channels are: '+str(self.quantization_channels))
        tf.logging.info('skip channels are: '+str(self.skip_channels))
        tf.logging.info('residual channels are: '+str(self.residual_channels))
        print('dilation structure:', self.dilations)


    def _create_variables(self):
        '''This function creates all variables used by the network.
        This allows us to share them between multiple calls to the loss
        function and generation function.'''

        var = dict()

        with tf.variable_scope('bytenet'):
            with tf.variable_scope('causal_layer'):
                layer = dict()
                if self.scalar_input:
                    initial_channels = self.quantization_channels
                    initial_filter_width = self.initial_filter_width
                else:
                    initial_channels = self.initial_channels
                    initial_filter_width = self.filter_width
                layer['filter'] = create_variable(
                    'filter',
                    [initial_filter_width, #this is pretty large
                     initial_channels,
                     self.residual_channels])
                var['causal_layer'] = layer

            var['dilated_stack'] = list()
            with tf.variable_scope('dilated_stack'):
                for i, dilation in enumerate(self.dilations): #notice here nick that there are indeed different variables for each stack as dialtions is a very very large list
                    with tf.variable_scope('layer{}'.format(i)):
                        current = dict()
                        current['filter'] = create_variable(
                            'filter',
                            [self.filter_width,
                             self.residual_channels,
                             self.dilation_channels])
                        current['gate'] = create_variable(
                            'gate',
                            [self.filter_width,
                             self.residual_channels,
                             self.dilation_channels])
                        current['dense'] = create_variable(
                            'dense',
                            [1,
                             self.dilation_channels,
                             self.residual_channels])
                        current['skip'] = create_variable(
                            'skip',
                            [1,
                             self.dilation_channels,
                             self.skip_channels])
                        if self.use_biases:
                            current['filter_bias'] = create_bias_variable(
                                'filter_bias',
                                [self.dilation_channels])
                            current['gate_bias'] = create_bias_variable(
                                'gate_bias',
                                [self.dilation_channels])
                            current['dense_bias'] = create_bias_variable(
                                'dense_bias',
                                [self.residual_channels])
                            current['skip_bias'] = create_bias_variable(
                                'slip_bias',
                                [self.skip_channels])

                        var['dilated_stack'].append(current)

        return var

    def _create_causal_layer(self, input_batch):
        '''Creates a single causal convolution layer.

        The layer can change the number of channels.
        '''
        with tf.name_scope('causal_layer'):
            weights_filter = self.variables['causal_layer']['filter']
            return causal_conv(input_batch, weights_filter, 1)

    def _generator_conv(self, input_batch, state_batch, weights):
        '''Perform convolution for a single convolutional processing step.'''
        # TODO generalize to filter_width > 2
        past_weights = weights[0, :, :]
        curr_weights = weights[1, :, :]
        output = tf.matmul(state_batch, past_weights) + tf.matmul(
            input_batch, curr_weights)
        return output

    def _generator_causal_layer(self, input_batch, state_batch):
        with tf.name_scope('causal_layer'):
            weights_filter = self.variables['causal_layer']['filter']
            output = self._generator_conv(
                input_batch, state_batch, weights_filter)
        return output

    def _generator_dilation_layer(self, input_batch, state_batch, layer_index,
                                  dilation):
        variables = self.variables['dilated_stack'][layer_index]

        weights_filter = variables['filter']
        weights_gate = variables['gate']
        output_filter = self._generator_conv(
            input_batch, state_batch, weights_filter)
        output_gate = self._generator_conv(
            input_batch, state_batch, weights_gate)

        if self.use_biases:
            output_filter = output_filter + variables['filter_bias']
            output_gate = output_gate + variables['gate_bias']

        out = tf.tanh(output_filter) * tf.sigmoid(output_gate)

        weights_dense = variables['dense']
        transformed = tf.matmul(out, weights_dense[0, :, :])
        if self.use_biases:
            transformed = transformed + variables['dense_bias']

        weights_skip = variables['skip']
        skip_contribution = tf.matmul(out, weights_skip[0, :, :])
        if self.use_biases:
            skip_contribution = skip_contribution + variables['skip_bias']

        return skip_contribution, input_batch + transformed

    def create_source_network(self, input_batch):
        '''creates a source network for bytenet -- does not use any causal masking layers or sub batch normalization'''

        current_layer = input_batch

        # Pre-process the input with a regular convolution
        if self.scalar_input:
            initial_channels = self.initial_channels
        else:
            initial_channels = self.quantization_channels

        current_layer = self._create_causal_layer(current_layer)

        with tf.name_scope('source_dilated_stack'):
            for layer_index, dilation in enumerate(self.dilations):
                with tf.name_scope('layer{}'.format(layer_index)):
                    current_layer = bytenet_ops.create_simple_bytenet_dilation_layer(
                        current_layer, layer_index, dilation, self.variables)
        return current_layer
                    # outputs.append(output) #these outputs are the summed skip connection


    def create_wavenet_network(self, input_batch):
        '''Construct the WaveNet network. -- another useful variation to test for source network'''
        outputs = []
        current_layer = input_batch

        # Pre-process the input with a regular convolution
        if self.scalar_input:
            initial_channels = self.initial_channels
        else:
            initial_channels = self.quantization_channels

        current_layer = self._create_causal_layer(current_layer)

        # Add all defined dilation layers.
        with tf.name_scope('dilated_stack'):
            for layer_index, dilation in enumerate(self.dilations):
                with tf.name_scope('layer{}'.format(layer_index)):
                    output, current_layer = self._create_dilation_layer(
                        current_layer, layer_index, dilation)
                    outputs.append(output) #these outputs are the summed skip connection

        with tf.name_scope('postprocessing'):
            # Perform (+) -> ReLU -> 1x1 conv -> ReLU -> 1x1 conv to
            # postprocess the output.
            # w1 = self.variables['postprocessing']['postprocess1']
            # if self.use_biases:
            #     b1 = self.variables['postprocessing']['postprocess1_bias']

            if self.histograms:
                tf.histogram_summary('postprocess1_weights', w1)
                if self.use_biases:
                    tf.histogram_summary('postprocess1_biases', b1)

            # We skip connections from the outputs of each layer, adding them
            # all up here.
            residual_total = sum(outputs)


            # transformed1 = tf.nn.relu(residual_total)
            # conv1 = tf.nn.conv1d(transformed1, w1, stride=1, padding="SAME")

        return residual_total

    def _create_generator(self, input_batch):
        '''Construct an efficient incremental generator. -- this is only for producing outputs'''
        init_ops = []
        push_ops = []
        outputs = []
        current_layer = input_batch

        q = tf.FIFOQueue(
            1,
            dtypes=tf.float32,
            shapes=(self.batch_size, self.quantization_channels))
        init = q.enqueue_many(
            tf.zeros((1, self.batch_size, self.quantization_channels)))

        current_state = q.dequeue()
        push = q.enqueue([current_layer])
        init_ops.append(init)
        push_ops.append(push)

        current_layer = self._generator_causal_layer(
                            current_layer, current_state)

        # Add all defined dilation layers.
        with tf.name_scope('dilated_stack'):
            for layer_index, dilation in enumerate(self.dilations):
                with tf.name_scope('layer{}'.format(layer_index)):

                    q = tf.FIFOQueue(
                        dilation,
                        dtypes=tf.float32,
                        shapes=(self.batch_size, self.residual_channels))
                    init = q.enqueue_many(
                        tf.zeros((dilation, self.batch_size,
                                  self.residual_channels)))

                    current_state = q.dequeue()
                    push = q.enqueue([current_layer])
                    init_ops.append(init)
                    push_ops.append(push)

                    output, current_layer = self._generator_dilation_layer(
                        current_layer, current_state, layer_index, dilation)
                    outputs.append(output)
        self.init_ops = init_ops
        self.push_ops = push_ops

        with tf.name_scope('postprocessing'):
            variables = self.variables['postprocessing']
            # Perform (+) -> ReLU -> 1x1 conv -> ReLU -> 1x1 conv to
            # postprocess the output.
            w1 = variables['postprocess1']
            if self.use_biases:
                b1 = variables['postprocess1_bias']

            # We skip connections from the outputs of each layer, adding them
            # all up here.
            total = sum(outputs)
            transformed1 = tf.nn.relu(total)

            conv1 = tf.matmul(transformed1, w1[0, :, :])
            if self.use_biases:
                conv1 = conv1 + b1
        return conv1

    def _one_hot(self, input_batch):
        '''One-hot encodes the waveform amplitudes.

        This allows the definition of the network as a categorical distribution
        over a finite set of possible amplitudes.
        '''
        with tf.name_scope('one_hot_encode'):
            encoded = tf.one_hot(
                input_batch,
                depth=self.quantization_channels,
                dtype=tf.float32)
            shape = [self.batch_size, -1, self.quantization_channels]
            encoded = tf.reshape(encoded, shape)
        return encoded

    def predict_proba(self, waveform, name='wavenet'):
        '''Computes the probability distribution of the next sample based on
        all samples in the input waveform.
        If you want to generate audio by feeding the output of the network back
        as an input, see predict_proba_incremental for a faster alternative.'''
        with tf.name_scope(name):
            if self.scalar_input:
                encoded = tf.cast(waveform, tf.float32)
                encoded = tf.reshape(encoded, [-1, 1])
            else:
                encoded = self._one_hot(waveform)
            raw_output = self._create_network(encoded)
            out = tf.reshape(raw_output, [-1, self.quantization_channels])
            # Cast to float64 to avoid bug in TensorFlow
            proba = tf.cast(
                tf.nn.softmax(tf.cast(out, tf.float64)), tf.float32)
            last = tf.slice(
                proba,
                [tf.shape(proba)[0] - 1, 0],
                [1, self.quantization_channels])
            return tf.reshape(last, [-1])

    def predict_proba_incremental(self, waveform, name='wavenet'):
        '''Computes the probability distribution of the next sample
        incrementally, based on a single sample and all previously passed
        samples.'''
        if self.filter_width > 2:
            raise NotImplementedError("Incremental generation does not "
                                      "support filter_width > 2.")
        if self.scalar_input:
            raise NotImplementedError("Incremental generation does not "
                                      "support scalar input yet.")
        with tf.name_scope(name):

            encoded = tf.one_hot(waveform, self.quantization_channels)
            encoded = tf.reshape(encoded, [-1, self.quantization_channels])
            raw_output = self._create_generator(encoded)
            out = tf.reshape(raw_output, [-1, self.quantization_channels])
            proba = tf.cast(
                tf.nn.softmax(tf.cast(out, tf.float64)), tf.float32)
            last = tf.slice(
                proba,
                [tf.shape(proba)[0] - 1, 0],
                [1, self.quantization_channels])
            return tf.reshape(last, [-1])

    
    def loss(self,
             input_batch,
             l2_regularization_strength=None,
             name='wavenet'):
        '''Creates a WaveNet network and returns the autoencoding loss.

        The variables are all scoped to the given name.
        '''
        with tf.name_scope(name):
            # We mu-law encode and quantize the input audioform.
            input_batch = mu_law_encode(input_batch,
                                        self.quantization_channels)
            encoded = self._one_hot(input_batch)

            if self.scalar_input:
                network_input = tf.reshape(
                    tf.cast(input_batch, tf.float32),
                    [self.batch_size, -1, 1])
            else:
                network_input = encoded

            raw_output = self._create_network(network_input)

            with tf.name_scope('loss'):
                # Shift original input left by one sample, which means that
                # each output sample has to predict the next input sample.
                shifted = tf.slice(encoded, [0, 1, 0],
                                   [-1, tf.shape(encoded)[1] - 1, -1])
                shifted = tf.pad(shifted, [[0, 0], [0, 1], [0, 0]])

                prediction = tf.reshape(raw_output,
                                        [-1, self.quantization_channels])
                loss = tf.nn.softmax_cross_entropy_with_logits(
                    prediction,
                    tf.reshape(shifted, [-1, self.quantization_channels]))
                reduced_loss = tf.reduce_mean(loss)

                tf.scalar_summary('loss', reduced_loss)

                if l2_regularization_strength is None:
                    return reduced_loss
                else:
                    # L2 regularization for all trainable parameters
                    l2_loss = tf.add_n([tf.nn.l2_loss(v)
                                        for v in tf.trainable_variables()
                                        if not('bias' in v.name)])

                    # Add the regularization term to the loss
                    total_loss = (reduced_loss +
                                  l2_regularization_strength * l2_loss)

                    tf.scalar_summary('l2_loss', l2_loss)
                    tf.scalar_summary('total_loss', total_loss)

                    return total_loss