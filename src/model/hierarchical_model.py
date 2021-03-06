import tensorflow as tf
from src.model import model_helper
import abc
import src.model


class HModel(src.model.BaseModel):
    """This class implements a hierarchical utterance classifier"""

    def compute_loss(self, logits):
        """The loss differs from that of the non-hierarchical model since in the hierarchical,
            we make one prediction per timestamp and we pad in the session level, so we also need to mask."""
        target_output = self.iterator.target
        crossent = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=target_output, logits=logits)
        mask = tf.sequence_mask(self.iterator.input_sess_length, target_output.shape[1].value,
                                dtype=logits.dtype)
        crossent_masked = tf.boolean_mask(crossent, mask)
        loss = tf.reduce_mean(crossent_masked)
        return loss

    def compute_accuracy(self, labels):
        target_output = self.iterator.target
        correct_pred = tf.equal(labels, target_output)
        correct_pred = tf.cast(correct_pred, tf.float32)
        mask = tf.sequence_mask(self.iterator.input_sess_length, target_output.shape[1].value)
        correct_pred_masked = tf.boolean_mask(correct_pred, mask)
        accuracy = tf.reduce_mean(correct_pred_masked)
        return accuracy

    def compute_labels(self, logits):
        return tf.argmax(self.compute_probabilities(logits), len(logits.get_shape()) - 1)

    def compute_probabilities(self, logits):
        return tf.nn.softmax(logits)

    def build_network(self, hparams):
        print ("Creating %s graph" % self.mode)
        dtype = tf.float32
        with tf.variable_scope("h_model", dtype=dtype) as scope:
            # reshape_input_emb.shape = [batch_size*num_utterances, uttr_max_len, embed_dim]
            reshape_input = tf.reshape(self.iterator.input, [-1, model_helper.get_tensor_dim(self.iterator.input, -1)])
            # utterances representation: utterances_embs.shape = [batch_size*num_utterances, uttr_units] or for bi:
            # [batch_size*num_utterances, uttr_units*2]
            utterances_embs = self.utterance_encoder(hparams, reshape_input)
            # reshape_utterances_embs.shape = [batch_size,  max_sess_length, uttr_units * 2] or
            # [batch_size, max_sess_length, uttr_units]
            reshape_utterances_embs = tf.reshape(utterances_embs, shape=[self.batch_size, model_helper.get_tensor_dim(
                self.iterator.input, 1),
                                                                         utterances_embs.get_shape()[-1]])
            # session rnn outputs: session_rnn_outputs.shape = [batch_size, max_sess_length, sess_units] or for bi:
            # [batch_size, max_sess_length, sess_units*2]
            session_rnn_outputs = self.session_encoder(hparams, reshape_utterances_embs)
            if hparams.connect_inp_to_out:
                session_rnn_outputs = tf.concat([reshape_utterances_embs, session_rnn_outputs], axis=-1)
            logits = self.output_layer(hparams, session_rnn_outputs)
            # compute loss
            if self.mode == tf.contrib.learn.ModeKeys.INFER:
                loss = None
            else:
                loss = self.compute_loss(logits)
            return logits, loss

    @abc.abstractmethod
    def utterance_encoder(self, hparams, input_emb):
        """All sub-classes should implement this method based on the utterance level model (e.g. RNN/CNN)."""
        pass

    @abc.abstractmethod
    def session_encoder(self, hparams, utterances_embs):
        """All sub-classes should implement this method based on the session level model (e.g. RNN)."""
        pass


class H_RNN(HModel):
    """Hierarchical model with RNN in the session level"""

    def session_encoder(self, hparams, utterances_embs):
        with tf.variable_scope("session_rnn") as scope:
            rnn_outputs, last_hidden_sate = model_helper.rnn_network(utterances_embs, scope.dtype,
                                                                     hparams.sess_rnn_type, hparams.sess_unit_type,
                                                                     hparams.sess_units, hparams.sess_layers,
                                                                     hparams.sess_hid_to_out_dropout,
                                                                     self.iterator.input_sess_length, hparams.forget_bias,
                                                                     hparams.sess_activation,
                                                                     self.mode)
        return rnn_outputs


class H_RNN_FFN(H_RNN):
    """Hierarchical Model with RNN in the session level and FFN in the utterance level."""

    def utterance_encoder(self, hparams, inputs):
        utterances_embs = model_helper.ffn(self.iterator.input, layers=hparams.uttr_layers,
                                           units_list=hparams.uttr_units, bias=True,
                                           hid_to_out_dropouts=hparams.uttr_hid_to_out_dropout,
                                           activations=hparams.uttr_activation, mode=self.mode)
        return utterances_embs


class H_RNN_RNN(H_RNN):
    """Hierarchical Model with RNN in the session level and RNN in the utterance level."""

    def init_embeddings(self, hparams):
        self.input_embedding, self.input_emb_init, self.input_emb_placeholder = model_helper.create_embeddings \
            (vocab_size=self.vocab_size,
             emb_size=hparams.input_emb_size,
             emb_trainable=hparams.input_emb_trainable,
             emb_pretrain=hparams.input_emb_pretrain)

    def utterance_encoder(self, hparams, inputs):
        self.vocab_size = hparams.vocab_size
        # Create embedding layer
        self.init_embeddings(hparams)
        emb_inp = tf.nn.embedding_lookup(self.input_embedding, inputs)
        with tf.variable_scope("utterance_rnn") as scope:
            reshape_uttr_length = tf.reshape(self.iterator.input_uttr_length, [-1])
            rnn_outputs, last_hidden_sate = model_helper.rnn_network(emb_inp, scope.dtype,
                                                                     hparams.uttr_rnn_type, hparams.uttr_unit_type,
                                                                     hparams.uttr_units, hparams.uttr_layers,
                                                                     hparams.uttr_hid_to_out_dropout,
                                                                     reshape_uttr_length,
                                                                     hparams.forget_bias, hparams.uttr_activation,
                                                                     self.mode)
            # utterances_embs.shape = [batch_size*num_utterances, uttr_units] or
            # [batch_size*num_utterances, 2*uttr_units]
            utterances_embs, self.attn_alphas = model_helper.pool_rnn_output(hparams.uttr_pooling, rnn_outputs,
                                                                             last_hidden_sate, reshape_uttr_length,
                                                                             hparams.uttr_attention_size)
        return utterances_embs


class H_RNN_CNN(H_RNN):
    """Hierarchical Model with RNN in the session level and CNN in the utterance level."""

    def init_embeddings(self, hparams):
        self.input_embedding, self.input_emb_init, self.input_emb_placeholder = model_helper.create_embeddings \
            (vocab_size=self.vocab_size,
             emb_size=hparams.input_emb_size,
             emb_trainable=hparams.input_emb_trainable,
             emb_pretrain=hparams.input_emb_pretrain)

    def utterance_encoder(self, hparams, inputs):
        self.vocab_size = hparams.vocab_size
        # Create embedding layer
        self.init_embeddings(hparams)
        emb_inp = tf.nn.embedding_lookup(self.input_embedding, inputs)
        emb_inp = tf.expand_dims(emb_inp, -1)
        with tf.variable_scope("utterance_cnn"):
            reshape_uttr_length = tf.reshape(self.iterator.input_uttr_length, [-1])
            filter_sizes = [(filter_size, hparams.input_emb_size) for filter_size in hparams.filter_sizes]
            cnn_outputs = model_helper.cnn(emb_inp, reshape_uttr_length, filter_sizes,
                                           hparams.num_filters, hparams.stride,
                                           hparams.uttr_activation[0], hparams.uttr_hid_to_out_dropout[0],
                                           self.mode, hparams.padding)
        return cnn_outputs


class H_RNN_RNN_CRF(H_RNN_RNN):
    """Hierarchical Model with RNN in the session level, RNN in the utterance level and CRF on top."""

    def _get_trans_params(self):
        return self.transition_params

    def compute_loss(self, logits):
        target_output = self.iterator.target
        log_likelihood, self.transition_params = tf.contrib.crf.crf_log_likelihood(logits, target_output,
                                                                                   self.iterator.input_sess_length)
        return tf.reduce_mean(-log_likelihood)

    def compute_probabilities(self, logits):
        return tf.no_op()

    def compute_labels(self, logits):
        viterbi_sequence, viterbi_score = tf.contrib.crf.crf_decode(self.logits, self.transition_params,
                                                                    self.iterator.input_sess_length)
        predictions = tf.convert_to_tensor(viterbi_sequence)  # , np.float32)
        return predictions
