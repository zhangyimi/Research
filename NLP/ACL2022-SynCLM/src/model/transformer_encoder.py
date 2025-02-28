#   Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Transformer encoder."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from functools import partial

import paddle.fluid as fluid
import paddle.fluid.layers as layers


def multi_head_attention(queries,
                         keys,
                         values,
                         attn_bias,
                         d_key,
                         d_value,
                         d_model,
                         n_head=1,
                         dropout_rate=0.,
                         cache=None,
                         gather_idx=None,
                         param_initializer=None,
                         name='multi_head_att'):
    """
    Multi-Head Attention. Note that attn_bias is added to the logit before
    computing softmax activiation to mask certain selected positions so that
    they will not considered in attention weights.
    """
    keys = queries if keys is None else keys
    values = keys if values is None else values

    if not (len(queries.shape) == len(keys.shape) == len(values.shape) == 3):
        raise ValueError("Inputs: quries, keys and values should all be 3-D tensors.")

    def __compute_qkv(queries, keys, values, n_head, d_key, d_value):
        """
        Add linear projection to queries, keys, and values.
        """
        q = layers.fc(input=queries,
                      size=d_key * n_head,
                      num_flatten_dims=2,
                      param_attr=fluid.ParamAttr(name=name + '_query_fc.w_0', initializer=param_initializer),
                      bias_attr=name + '_query_fc.b_0')
        k = layers.fc(input=keys,
                      size=d_key * n_head,
                      num_flatten_dims=2,
                      param_attr=fluid.ParamAttr(name=name + '_key_fc.w_0', initializer=param_initializer),
                      bias_attr=name + '_key_fc.b_0')
        v = layers.fc(input=values,
                      size=d_value * n_head,
                      num_flatten_dims=2,
                      param_attr=fluid.ParamAttr(name=name + '_value_fc.w_0', initializer=param_initializer),
                      bias_attr=name + '_value_fc.b_0')
        return q, k, v

    def __split_heads(x, n_head):
        """
        Reshape the last dimension of inpunt tensor x so that it becomes two
        dimensions and then transpose. Specifically, input a tensor with shape
        [bs, max_sequence_length, n_head * hidden_dim] then output a tensor
        with shape [bs, n_head, max_sequence_length, hidden_dim].
        """
        hidden_size = x.shape[-1]
        # The value 0 in shape attr means copying the corresponding dimension
        # size of the input as the output dimension size.
        reshaped = layers.reshape(x=x, shape=[0, 0, n_head, hidden_size // n_head], inplace=True)

        # permuate the dimensions into:
        # [batch_size, n_head, max_sequence_len, hidden_size_per_head]
        return layers.transpose(x=reshaped, perm=[0, 2, 1, 3])

    def __combine_heads(x):
        """
        Transpose and then reshape the last two dimensions of inpunt tensor x
        so that it becomes one dimension, which is reverse to __split_heads.
        """
        if len(x.shape) == 3:
            return x
        if len(x.shape) != 4:
            raise ValueError("Input(x) should be a 4-D Tensor.")

        trans_x = layers.transpose(x, perm=[0, 2, 1, 3])
        # The value 0 in shape attr means copying the corresponding dimension
        # size of the input as the output dimension size.
        return layers.reshape(x=trans_x, shape=[0, 0, trans_x.shape[2] * trans_x.shape[3]], inplace=True)

    def scaled_dot_product_attention(q, k, v, attn_bias, d_key, dropout_rate):
        """
        Scaled Dot-Product Attention
        """
        scaled_q = layers.scale(x=q, scale=d_key**-0.5)
        product = layers.matmul(x=scaled_q, y=k, transpose_y=True)
        if attn_bias:
            product += attn_bias
        weights = layers.softmax(product)
        if dropout_rate:
            weights = layers.dropout(weights,
                                     dropout_prob=dropout_rate,
                                     dropout_implementation="upscale_in_train",
                                     is_test=False)
        out = layers.matmul(weights, v)
        return out, weights

    q, k, v = __compute_qkv(queries, keys, values, n_head, d_key, d_value)

    if cache is not None:  # use cache and concat time steps
        # Since the inplace reshape in __split_heads changes the shape of k and
        # v, which is the cache input for next time step, reshape the cache
        # input from the previous time step first.

        cache_k, cache_v = cache["k"], cache["v"]
        select_k = layers.gather(cache_k, index=gather_idx)
        select_v = layers.gather(cache_v, index=gather_idx)
        select_k = layers.reshape(select_k, shape=[0, 0, d_model])
        select_v = layers.reshape(select_v, shape=[0, 0, d_model])
        k = layers.concat([select_k, k], axis=1)
        v = layers.concat([select_v, v], axis=1)
        layers.assign(k, cache["k"])
        layers.assign(v, cache["v"])

    q = __split_heads(q, n_head)
    k = __split_heads(k, n_head)
    v = __split_heads(v, n_head)

    ctx_multiheads, attention = scaled_dot_product_attention(q, k, v, attn_bias, d_key, dropout_rate)

    out = __combine_heads(ctx_multiheads)

    # Project back to the model size.
    proj_out = layers.fc(input=out,
                         size=d_model,
                         num_flatten_dims=2,
                         param_attr=fluid.ParamAttr(name=name + '_output_fc.w_0', initializer=param_initializer),
                         bias_attr=name + '_output_fc.b_0')
    return proj_out, attention


def pos_multi_head_attention(queries,
                             keys,
                             values,
                             pos_out,
                             attn_bias,
                             d_key,
                             d_value,
                             d_model,
                             n_head=1,
                             dropout_rate=0.,
                             cache=None,
                             gather_idx=None,
                             param_initializer=None,
                             name='multi_head_att'):
    """
    Multi-Head Attention. Note that attn_bias is added to the logit before
    computing softmax activiation to mask certain selected positions so that
    they will not considered in attention weights.
    """
    keys = queries if keys is None else keys
    values = keys if values is None else values

    if not (len(queries.shape) == len(keys.shape) == len(values.shape) == 3):
        raise ValueError("Inputs: quries, keys and values should all be 3-D tensors.")

    def __compute_qkv(queries, keys, values, n_head, d_key, d_value, pos_out):
        """
        Add linear projection to queries, keys, and values.
        """
        q = layers.fc(input=queries,
                      size=d_key * n_head,
                      num_flatten_dims=2,
                      param_attr=fluid.ParamAttr(name=name + '_query_fc.w_0', initializer=param_initializer),
                      bias_attr=name + '_query_fc.b_0')
        k = layers.fc(input=keys,
                      size=d_key * n_head,
                      num_flatten_dims=2,
                      param_attr=fluid.ParamAttr(name=name + '_key_fc.w_0', initializer=param_initializer),
                      bias_attr=name + '_key_fc.b_0')
        v = layers.fc(input=values,
                      size=d_value * n_head,
                      num_flatten_dims=2,
                      param_attr=fluid.ParamAttr(name=name + '_value_fc.w_0', initializer=param_initializer),
                      bias_attr=name + '_value_fc.b_0')
        # p = layers.fc(input=pos_out,
        #               size=d_value * n_head,
        #               num_flatten_dims=2,
        #               param_attr=fluid.ParamAttr(name=name + '_pos_fc.w_0', initializer=param_initializer),
        #               bias_attr=name + '_pos_fc.b_0')
        p = pos_out
        return q, k, v, p

    def __split_heads(x, n_head):
        """
        Reshape the last dimension of inpunt tensor x so that it becomes two
        dimensions and then transpose. Specifically, input a tensor with shape
        [bs, max_sequence_length, n_head * hidden_dim] then output a tensor
        with shape [bs, n_head, max_sequence_length, hidden_dim].
        """
        hidden_size = x.shape[-1]
        # The value 0 in shape attr means copying the corresponding dimension
        # size of the input as the output dimension size.
        reshaped = layers.reshape(x=x, shape=[0, 0, n_head, hidden_size // n_head], inplace=True)

        # permuate the dimensions into:
        # [batch_size, n_head, max_sequence_len, hidden_size_per_head]
        return layers.transpose(x=reshaped, perm=[0, 2, 1, 3])

    def __combine_heads(x):
        """
        Transpose and then reshape the last two dimensions of inpunt tensor x
        so that it becomes one dimension, which is reverse to __split_heads.
        """
        if len(x.shape) == 3:
            return x
        if len(x.shape) != 4:
            raise ValueError("Input(x) should be a 4-D Tensor.")

        trans_x = layers.transpose(x, perm=[0, 2, 1, 3])
        # The value 0 in shape attr means copying the corresponding dimension
        # size of the input as the output dimension size.
        return layers.reshape(x=trans_x, shape=[0, 0, trans_x.shape[2] * trans_x.shape[3]], inplace=True)

    def scaled_dot_product_attention(q, k, v, p, attn_bias, d_key, dropout_rate):
        """
        Scaled Dot-Product Attention
        """
        # [batch, heads, len, hidden]
        scaled_q = layers.scale(x=q, scale=d_key**-0.5)
        # [batch, heads, len, len]
        product_k = layers.matmul(x=scaled_q, y=k, transpose_y=True)
        # [batch, heads, len, 1, hidden]
        r_scaled_q = fluid.layers.unsqueeze(input=scaled_q, axes=[3])
        # p: [batch, heads, len, len, hidden]
        # product_p: [batch, heads, len, 1, len]
        product_p = layers.matmul(x=r_scaled_q, y=p, transpose_y=True)
        product_p = fluid.layers.squeeze(product_p, axes=[3])
        product = product_k + product_p

        if attn_bias:
            product += attn_bias
        weights = layers.softmax(product)
        if dropout_rate:
            weights = layers.dropout(weights,
                                     dropout_prob=dropout_rate,
                                     dropout_implementation="upscale_in_train",
                                     is_test=False)
        out_v = layers.matmul(weights, v)
        r_weights = fluid.layers.unsqueeze(input=weights, axes=[3])
        out_p = layers.matmul(r_weights, p)
        out_p = fluid.layers.squeeze(out_p, axes=[3])
        out = out_v + out_p
        return out, weights

    q, k, v, p = __compute_qkv(queries, keys, values, n_head, d_key, d_value, pos_out)

    if cache is not None:  # use cache and concat time steps
        # Since the inplace reshape in __split_heads changes the shape of k and
        # v, which is the cache input for next time step, reshape the cache
        # input from the previous time step first.

        cache_k, cache_v = cache["k"], cache["v"]
        select_k = layers.gather(cache_k, index=gather_idx)
        select_v = layers.gather(cache_v, index=gather_idx)
        select_k = layers.reshape(select_k, shape=[0, 0, d_model])
        select_v = layers.reshape(select_v, shape=[0, 0, d_model])
        k = layers.concat([select_k, k], axis=1)
        v = layers.concat([select_v, v], axis=1)
        layers.assign(k, cache["k"])
        layers.assign(v, cache["v"])

    q = __split_heads(q, n_head)
    k = __split_heads(k, n_head)
    v = __split_heads(v, n_head)

    ctx_multiheads, attention = scaled_dot_product_attention(q, k, v, p, attn_bias, d_key, dropout_rate)

    out = __combine_heads(ctx_multiheads)

    # Project back to the model size.
    proj_out = layers.fc(input=out,
                         size=d_model,
                         num_flatten_dims=2,
                         param_attr=fluid.ParamAttr(name=name + '_output_fc.w_0', initializer=param_initializer),
                         bias_attr=name + '_output_fc.b_0')
    return proj_out, attention


def positionwise_feed_forward(x, d_inner_hid, d_hid, dropout_rate, hidden_act, param_initializer=None, name='ffn'):
    """
    Position-wise Feed-Forward Networks.
    This module consists of two linear transformations with a ReLU activation
    in between, which is applied to each position separately and identically.
    """
    hidden = layers.fc(input=x,
                       size=d_inner_hid,
                       num_flatten_dims=2,
                       act=hidden_act,
                       param_attr=fluid.ParamAttr(name=name + '_fc_0.w_0', initializer=param_initializer),
                       bias_attr=name + '_fc_0.b_0')
    if dropout_rate:
        hidden = layers.dropout(hidden,
                                dropout_prob=dropout_rate,
                                dropout_implementation="upscale_in_train",
                                is_test=False)
    out = layers.fc(input=hidden,
                    size=d_hid,
                    num_flatten_dims=2,
                    param_attr=fluid.ParamAttr(name=name + '_fc_1.w_0', initializer=param_initializer),
                    bias_attr=name + '_fc_1.b_0')
    return out


def pre_post_process_layer(prev_out, out, process_cmd, dropout_rate=0., name=''):
    """
    Add residual connection, layer normalization and droput to the out tensor
    optionally according to the value of process_cmd.
    This will be used before or after multi-head attention and position-wise
    feed-forward networks.
    """
    for cmd in process_cmd:
        if cmd == "a":  # add residual connection
            out = out + prev_out if prev_out else out
        elif cmd == "n":  # add layer normalization
            out = layers.layer_norm(out,
                                    begin_norm_axis=len(out.shape) - 1,
                                    param_attr=fluid.ParamAttr(name=name + '_layer_norm_scale',
                                                               initializer=fluid.initializer.Constant(1.)),
                                    bias_attr=fluid.ParamAttr(name=name + '_layer_norm_bias',
                                                              initializer=fluid.initializer.Constant(0.)))
        elif cmd == "d":  # add dropout
            if dropout_rate:
                out = layers.dropout(out,
                                     dropout_prob=dropout_rate,
                                     dropout_implementation="upscale_in_train",
                                     is_test=False)
    return out


pre_process_layer = partial(pre_post_process_layer, None)
post_process_layer = pre_post_process_layer


def pos_encoder_layer(query_input,
                      pos_out,
                      key_input,
                      attn_bias,
                      n_head,
                      d_key,
                      d_value,
                      d_model,
                      d_inner_hid,
                      prepostprocess_dropout,
                      attention_dropout,
                      relu_dropout,
                      hidden_act,
                      preprocess_cmd="n",
                      postprocess_cmd="da",
                      param_initializer=None,
                      name='',
                      cache=None,
                      gather_idx=None):
    """The encoder layers that can be stacked to form a deep encoder.
    This module consits of a multi-head (self) attention followed by
    position-wise feed-forward networks and both the two components companied
    with the post_process_layer to add residual connection, layer normalization
    and droput.
    """
    key_input = pre_process_layer(key_input, preprocess_cmd, prepostprocess_dropout, name=name +
                                  '_pre_att') if key_input else None
    value_input = key_input if key_input else None

    attn_output, att_mat = pos_multi_head_attention(pre_process_layer(query_input,
                                                                      preprocess_cmd,
                                                                      prepostprocess_dropout,
                                                                      name=name + '_pre_att'),
                                                    key_input,
                                                    value_input,
                                                    pos_out,
                                                    attn_bias,
                                                    d_key,
                                                    d_value,
                                                    d_model,
                                                    n_head,
                                                    attention_dropout,
                                                    param_initializer=param_initializer,
                                                    name=name + '_multi_head_att',
                                                    cache=cache,
                                                    gather_idx=gather_idx)
    attn_output = post_process_layer(query_input,
                                     attn_output,
                                     postprocess_cmd,
                                     prepostprocess_dropout,
                                     name=name + '_post_att')
    ffd_output = positionwise_feed_forward(pre_process_layer(attn_output,
                                                             preprocess_cmd,
                                                             prepostprocess_dropout,
                                                             name=name + '_pre_ffn'),
                                           d_inner_hid,
                                           d_model,
                                           relu_dropout,
                                           hidden_act,
                                           param_initializer=param_initializer,
                                           name=name + '_ffn')
    return post_process_layer(attn_output, ffd_output, postprocess_cmd, prepostprocess_dropout,
                              name=name + '_post_ffn'), att_mat


def encoder_layer(query_input,
                  key_input,
                  attn_bias,
                  n_head,
                  d_key,
                  d_value,
                  d_model,
                  d_inner_hid,
                  prepostprocess_dropout,
                  attention_dropout,
                  relu_dropout,
                  hidden_act,
                  preprocess_cmd="n",
                  postprocess_cmd="da",
                  param_initializer=None,
                  name='',
                  cache=None,
                  gather_idx=None):
    """The encoder layers that can be stacked to form a deep encoder.
    This module consits of a multi-head (self) attention followed by
    position-wise feed-forward networks and both the two components companied
    with the post_process_layer to add residual connection, layer normalization
    and droput.
    """
    key_input = pre_process_layer(key_input, preprocess_cmd, prepostprocess_dropout, name=name +
                                  '_pre_att') if key_input else None
    value_input = key_input if key_input else None

    attn_output, att_mat = multi_head_attention(pre_process_layer(query_input,
                                                                  preprocess_cmd,
                                                                  prepostprocess_dropout,
                                                                  name=name + '_pre_att'),
                                                key_input,
                                                value_input,
                                                attn_bias,
                                                d_key,
                                                d_value,
                                                d_model,
                                                n_head,
                                                attention_dropout,
                                                param_initializer=param_initializer,
                                                name=name + '_multi_head_att',
                                                cache=cache,
                                                gather_idx=gather_idx)
    attn_output = post_process_layer(query_input,
                                     attn_output,
                                     postprocess_cmd,
                                     prepostprocess_dropout,
                                     name=name + '_post_att')
    ffd_output = positionwise_feed_forward(pre_process_layer(attn_output,
                                                             preprocess_cmd,
                                                             prepostprocess_dropout,
                                                             name=name + '_pre_ffn'),
                                           d_inner_hid,
                                           d_model,
                                           relu_dropout,
                                           hidden_act,
                                           param_initializer=param_initializer,
                                           name=name + '_ffn')
    return post_process_layer(attn_output, ffd_output, postprocess_cmd, prepostprocess_dropout,
                              name=name + '_post_ffn'), att_mat


def encoder(enc_input,
            attn_bias,
            n_layer,
            n_head,
            d_key,
            d_value,
            d_model,
            d_inner_hid,
            prepostprocess_dropout,
            attention_dropout,
            relu_dropout,
            hidden_act,
            preprocess_cmd="n",
            postprocess_cmd="da",
            param_initializer=None,
            name='',
            caches=None,
            gather_idx=None):
    """
    The encoder is composed of a stack of identical layers returned by calling
    encoder_layer.
    """
    att_mats = []
    for i in range(n_layer):
        enc_output, att_mat = encoder_layer(enc_input,
                                            None,
                                            attn_bias,
                                            n_head,
                                            d_key,
                                            d_value,
                                            d_model,
                                            d_inner_hid,
                                            prepostprocess_dropout,
                                            attention_dropout,
                                            relu_dropout,
                                            hidden_act,
                                            preprocess_cmd,
                                            postprocess_cmd,
                                            param_initializer=param_initializer,
                                            name=name + '_layer_' + str(i),
                                            cache=caches[i] if caches is not None else None,
                                            gather_idx=gather_idx)
        enc_input = enc_output
        att_mats.append(att_mat)

    enc_output = pre_process_layer(enc_output, preprocess_cmd, prepostprocess_dropout, name="post_encoder")

    return enc_output, att_mats


def pos_encoder(enc_input,
                pos_out,
                attn_bias,
                n_layer,
                n_head,
                d_key,
                d_value,
                d_model,
                d_inner_hid,
                prepostprocess_dropout,
                attention_dropout,
                relu_dropout,
                hidden_act,
                preprocess_cmd="n",
                postprocess_cmd="da",
                param_initializer=None,
                name='',
                caches=None,
                gather_idx=None):
    """
    The encoder is composed of a stack of identical layers returned by calling
    encoder_layer.
    """
    att_mats = []

    def __split_heads_for_q(x, n_head):
        """
        Reshape the last dimension of inpunt tensor x so that it becomes two
        dimensions and then transpose. Specifically, input a tensor with shape
        [bs, max_sequence_length, n_head * hidden_dim] then output a tensor
        with shape [bs, n_head, max_sequence_length, hidden_dim].
        """
        hidden_size = x.shape[-1]
        # The value 0 in shape attr means copying the corresponding dimension
        # size of the input as the output dimension size.
        reshaped = layers.reshape(x=x, shape=[0, 0, 0, n_head, hidden_size // n_head], inplace=True)

        # permuate the dimensions into:
        # [batch_size, n_head, max_sequence_len, hidden_size_per_head]
        return layers.transpose(x=reshaped, perm=[0, 3, 1, 2, 4])

    pos_out = __split_heads_for_q(pos_out, 12)
    for i in range(n_layer):
        enc_output, att_mat = pos_encoder_layer(enc_input,
                                                pos_out,
                                                None,
                                                attn_bias,
                                                n_head,
                                                d_key,
                                                d_value,
                                                d_model,
                                                d_inner_hid,
                                                prepostprocess_dropout,
                                                attention_dropout,
                                                relu_dropout,
                                                hidden_act,
                                                preprocess_cmd,
                                                postprocess_cmd,
                                                param_initializer=param_initializer,
                                                name=name + '_layer_' + str(i),
                                                cache=caches[i] if caches is not None else None,
                                                gather_idx=gather_idx)
        enc_input = enc_output
        att_mats.append(att_mat)

    enc_output = pre_process_layer(enc_output, preprocess_cmd, prepostprocess_dropout, name=f"post_encoder_layer_{i}")

    return enc_output, att_mats
