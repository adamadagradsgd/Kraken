# -*- coding:utf-8 -*-
"""

Author:
    Weichen Shen,wcshen1994@163.com

"""
from __future__ import print_function

import time

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.data as Data
from sklearn.metrics import *
from torch.utils.data import DataLoader
from tqdm import tqdm

from ..inputs import build_input_features, SparseFeat, DenseFeat, VarLenSparseFeat, get_varlen_pooling_list, \
    create_embedding_matrix
from ..layers import PredictionLayer
from ..layers.utils import slice_arrays


class Linear(nn.Module):
    def __init__(self, feature_columns, feature_index, init_std=0.0001, device='cpu'):
        super(Linear, self).__init__()
        self.feature_index = feature_index
        self.device = device
        self.sparse_feature_columns = list(
            filter(lambda x: isinstance(x, SparseFeat), feature_columns)) if len(feature_columns) else []
        self.dense_feature_columns = list(
            filter(lambda x: isinstance(x, DenseFeat), feature_columns)) if len(feature_columns) else []

        self.varlen_sparse_feature_columns = list(
            filter(lambda x: isinstance(x, VarLenSparseFeat), feature_columns)) if len(feature_columns) else []

        self.embedding_dict = create_embedding_matrix(feature_columns, init_std, linear=True, sparse=False,
                                                      device=device)

        #         nn.ModuleDict(
        #             {feat.embedding_name: nn.Embedding(feat.dimension, 1, sparse=True) for feat in
        #              self.sparse_feature_columns}
        #         )
        # .to("cuda:1")
        for tensor in self.embedding_dict.values():
            nn.init.normal_(tensor.weight, mean=0, std=init_std)

        if len(self.dense_feature_columns) > 0:
            self.weight = nn.Parameter(torch.Tensor(sum(fc.dimension for fc in self.dense_feature_columns), 1)).to(
                device)
            torch.nn.init.normal_(self.weight, mean=0, std=init_std)

    def forward(self, X):

        sparse_embedding_list = [self.embedding_dict[feat.embedding_name](
            X[:, self.feature_index[feat.name][0]:self.feature_index[feat.name][1]].long()) for
            feat in self.sparse_feature_columns]

        dense_value_list = [X[:, self.feature_index[feat.name][0]:self.feature_index[feat.name][1]] for feat in
                            self.dense_feature_columns]

        varlen_embedding_list = get_varlen_pooling_list(self.embedding_dict, X, self.feature_index,
                                                        self.varlen_sparse_feature_columns, self.device)

        sparse_embedding_list += varlen_embedding_list

        if len(sparse_embedding_list) > 0 and len(dense_value_list) > 0:
            linear_sparse_logit = torch.sum(
                torch.cat(sparse_embedding_list, dim=-1), dim=-1, keepdim=False)
            linear_dense_logit = torch.cat(
                dense_value_list, dim=-1).matmul(self.weight)
            linear_logit = linear_sparse_logit + linear_dense_logit
        elif len(sparse_embedding_list) > 0:
            linear_logit = torch.sum(
                torch.cat(sparse_embedding_list, dim=-1), dim=-1, keepdim=False)
        elif len(dense_value_list) > 0:
            linear_logit = torch.cat(
                dense_value_list, dim=-1).matmul(self.weight)
        else:
            linear_logit = torch.zeros([X.shape[0], 1])
        return linear_logit


class BaseModel(nn.Module):
    def __init__(self,
                 linear_feature_columns, dnn_feature_columns, dnn_hidden_units=(
                     128, 128),
                 l2_reg_linear=1e-5,
                 l2_reg_embedding=1e-5, l2_reg_dnn=0, init_std=0.0001, seed=1024, dnn_dropout=0, dnn_activation='relu',
                 task='binary', device='cpu'):

        super(BaseModel, self).__init__()

        self.dnn_feature_columns = dnn_feature_columns

        self.reg_loss = torch.zeros((1,), device=device)
        self.aux_loss = torch.zeros((1,), device=device)
        self.device = device  # device

        self.feature_index = build_input_features(
            linear_feature_columns + dnn_feature_columns)
        self.dnn_feature_columns = dnn_feature_columns

        self.embedding_dict = create_embedding_matrix(
            dnn_feature_columns, init_std, sparse=False, device=device)
        #         nn.ModuleDict(
        #             {feat.embedding_name: nn.Embedding(feat.dimension, embedding_size, sparse=True) for feat in
        #              self.dnn_feature_columns}
        #         )

        self.linear_model = Linear(
            linear_feature_columns, self.feature_index, device=device)

        self.add_regularization_loss(
            self.embedding_dict.parameters(), l2_reg_embedding)
        self.add_regularization_loss(
            self.linear_model.parameters(), l2_reg_linear)

        self.out = PredictionLayer(task, )
        self.to(device)

    def fit(self, x=None,
            y=None,
            batch_size=None,
            epochs=1,
            verbose=1,
            initial_epoch=0,
            validation_split=0.,
            validation_data=None,
            shuffle=True,
            use_double=True,
            model_name="xmh_test",
            verbose_steps=500,
            xmh_model_dir=""):
        """
        :param x: Numpy array of training data (if the model has a single input), or list of Numpy arrays (if the model has multiple inputs).If input layers in the model are named, you can also pass a
            dictionary mapping input names to Numpy arrays.
        :param y: Numpy array of target (label) data (if the model has a single output), or list of Numpy arrays (if the model has multiple outputs).
        :param batch_size: Integer or `None`. Number of samples per gradient update. If unspecified, `batch_size` will default to 256.
        :param epochs: Integer. Number of epochs to train the model. An epoch is an iteration over the entire `x` and `y` data provided. Note that in conjunction with `initial_epoch`, `epochs` is to be understood as "final epoch". The model is not trained for a number of iterations given by `epochs`, but merely until the epoch of index `epochs` is reached.
        :param verbose: Integer. 0, 1, or 2. Verbosity mode. 0 = silent, 1 = progress bar, 2 = one line per epoch.
        :param initial_epoch: Integer. Epoch at which to start training (useful for resuming a previous training run).
        :param validation_split: Float between 0 and 1. Fraction of the training data to be used as validation data. The model will set apart this fraction of the training data, will not train on it, and will evaluate the loss and any model metrics on this data at the end of each epoch. The validation data is selected from the last samples in the `x` and `y` data provided, before shuffling.
        :param validation_data: tuple `(x_val, y_val)` or tuple `(x_val, y_val, val_sample_weights)` on which to evaluate the loss and any model metrics at the end of each epoch. The model will not be trained on this data. `validation_data` will override `validation_split`.
        :param shuffle: Boolean. Whether to shuffle the order of the batches at the beginning of each epoch.
        :param use_double: Boolean. Whether to use double precision in metric calculation.

        """
        if isinstance(x, dict):
            x = [x[feature] for feature in self.feature_index]
        if validation_data:
            if len(validation_data) == 2:
                val_x, val_y = validation_data
                val_sample_weight = None
            elif len(validation_data) == 3:
                val_x, val_y, val_sample_weight = validation_data  # pylint: disable=unpacking-non-sequence
            else:
                raise ValueError(
                    'When passing a `validation_data` argument, '
                    'it must contain either 2 items (x_val, y_val), '
                    'or 3 items (x_val, y_val, val_sample_weights), '
                    'or alternatively it could be a dataset or a '
                    'dataset or a dataset iterator. '
                    'However we received `validation_data=%s`' % validation_data)
            if isinstance(val_x, dict):
                val_x = [val_x[feature] for feature in self.feature_index]

        elif validation_split and 0. < validation_split < 1.:
            if hasattr(x[0], 'shape'):
                split_at = int(x[0].shape[0] * (1. - validation_split))
            else:
                split_at = int(len(x[0]) * (1. - validation_split))
            x, val_x = (slice_arrays(x, 0, split_at),
                        slice_arrays(x, split_at))
            y, val_y = (slice_arrays(y, 0, split_at),
                        slice_arrays(y, split_at))

        else:
            val_x = []
            val_y = []
        for i in range(len(x)):
            if len(x[i].shape) == 1:
                x[i] = np.expand_dims(x[i], axis=1)

        train_tensor_data = Data.TensorDataset(
            torch.from_numpy(
                np.concatenate(x, axis=-1)),
            torch.from_numpy(y))
        if batch_size is None:
            batch_size = 256
        train_loader = DataLoader(
            dataset=train_tensor_data, shuffle=shuffle, batch_size=batch_size)

        from torch.utils.tensorboard import SummaryWriter

        import re
        import datetime
        writer = SummaryWriter(xmh_model_dir)

        print(self.device, end="\n")
        model = self.train()
        loss_func = self.loss_func
        optim = self.optim
        optim_s = self.optim_s

        sample_num = len(train_tensor_data)
        best_val_auc= 0
        verboses_no_improve  = 0
        steps_per_epoch = (sample_num - 1) // batch_size + 1

        print("Train on {0} samples, validate on {1} samples, {2} steps per epoch".format(
            len(train_tensor_data), len(val_y), steps_per_epoch))
        for epoch in range(initial_epoch, epochs):
            start_time = time.time()
            loss_epoch = 0
            total_loss_epoch = 0
            # if abs(loss_last - loss_now) < 0.0
            train_result = {}
            try:
                with tqdm(enumerate(train_loader), disable=True) as t:
                    for index, (x_train, y_train) in t:
                        x = x_train.to(self.device).float()
                        y = y_train.to(self.device).float()

                        optim.zero_grad()
                        if optim_s is not None:
                            optim_s.zero_grad()

                        y_pred = model(x).squeeze()
                        loss = loss_func(y_pred, y.squeeze(), reduction='sum')

                        total_loss = loss + self.reg_loss + self.aux_loss

                        loss_epoch += loss.item()
                        total_loss_epoch += total_loss.item()
                        total_loss.backward(retain_graph=True)

                        optim.step()
                        if optim_s is not None:
                            optim_s.step()

                        if verbose > 0:
                            for name, metric_fun in self.metrics.items():
                                if name not in train_result:
                                    train_result[name] = []

                                if use_double:
                                    train_result[name].append(metric_fun(
                                        y.cpu().data.numpy(), y_pred.cpu().data.numpy().astype("float64")))
                                else:
                                    temp = metric_fun(
                                        y.cpu().data.numpy(), y_pred.cpu().data.numpy())
                                    train_result[name].append(temp)

                        if verbose > 0 and index % verbose_steps == (verbose_steps-1):
                            eval_str = "[Iter{0}] - loss: {1: .4f}".format(
                                index, total_loss_epoch / ((index+1) * batch_size))

                            for name, result in train_result.items():
                                eval_str += " - " + name + \
                                            ": {0: .4f}".format(
                                                np.sum(result) / (index+1))
                                writer.add_scalar(
                                    '{0}/train'.format(name), np.sum(result) / (index+1), index+steps_per_epoch*epoch)

                            if len(val_x) and len(val_y):
                                eval_result = self.evaluate(
                                    val_x, val_y, 20480)
                                if eval_result['auc'] < 0.6:
                                    print("something failed")
                                    break
                                if eval_result['auc'] > best_val_auc:
                                    best_val_auc = eval_result['auc']
                                    verboses_no_improve = 0
                                else:
                                    verboses_no_improve += 1
                                    if verboses_no_improve == 3:
                                        print("early stop")
                                        break


                                for name, result in eval_result.items():
                                    eval_str += " - val_" + name + \
                                                ": {0: .4f}".format(result)
                                    writer.add_scalar(
                                        '{0}/val'.format(name), result, index+steps_per_epoch*epoch)
                            print(eval_str)

            except KeyboardInterrupt:
                t.close()
                raise
            t.close()

            epoch_time = int(time.time() - start_time)
            if verbose > 0:
                print('Epoch {0}/{1}'.format(epoch + 1, epochs))

                eval_str = "{0}s - loss: {1: .4f}".format(
                    epoch_time, total_loss_epoch / sample_num)

                for name, result in train_result.items():
                    eval_str += " - " + name + \
                                ": {0: .4f}".format(
                                    np.sum(result) / steps_per_epoch)

                if len(val_x) and len(val_y):
                    eval_result = self.evaluate(val_x, val_y, batch_size)

                    for name, result in eval_result.items():
                        eval_str += " - val_" + name + \
                                    ": {0: .4f}".format(result)
                print(eval_str)
        writer.close()

    def evaluate(self, x, y, batch_size=20480):
        """

        :param x: Numpy array of test data (if the model has a single input), or list of Numpy arrays (if the model has multiple inputs).
        :param y: Numpy array of target (label) data (if the model has a single output), or list of Numpy arrays (if the model has multiple outputs).
        :param batch_size:
        :return: Integer or `None`. Number of samples per evaluation step. If unspecified, `batch_size` will default to 256.
        """
        pred_ans = self.predict(x, batch_size)
        eval_result = {}
        for name, metric_fun in self.metrics.items():
            eval_result[name] = metric_fun(y, pred_ans)
        return eval_result

    def predict(self, x, batch_size=256, use_double=True):
        """

        :param x: The input data, as a Numpy array (or list of Numpy arrays if the model has multiple inputs).
        :param batch_size: Integer. If unspecified, it will default to 256.
        :return: Numpy array(s) of predictions.
        """
        model = self.eval()
        if isinstance(x, dict):
            x = [x[feature] for feature in self.feature_index]
        for i in range(len(x)):
            if len(x[i].shape) == 1:
                x[i] = np.expand_dims(x[i], axis=1)

        tensor_data = Data.TensorDataset(
            torch.from_numpy(np.concatenate(x, axis=-1)))
        test_loader = DataLoader(
            dataset=tensor_data, shuffle=False, batch_size=batch_size)

        pred_ans = []
        with torch.no_grad():
            for index, x_test in enumerate(test_loader):
                x = x_test[0].to(self.device).float()
                # y = y_test.to(self.device).float()

                y_pred = model(x).cpu().data.numpy()  # .squeeze()
                pred_ans.append(y_pred)

        if use_double:
            return np.concatenate(pred_ans).astype("float64")
        else:
            return np.concatenate(pred_ans)

    def input_from_feature_columns(self, X, feature_columns, embedding_dict, support_dense=True):

        sparse_feature_columns = list(
            filter(lambda x: isinstance(x, SparseFeat), feature_columns)) if len(feature_columns) else []
        dense_feature_columns = list(
            filter(lambda x: isinstance(x, DenseFeat), feature_columns)) if len(feature_columns) else []

        varlen_sparse_feature_columns = list(
            filter(lambda x: isinstance(x, VarLenSparseFeat), feature_columns)) if feature_columns else []

        if not support_dense and len(dense_feature_columns) > 0:
            raise ValueError(
                "DenseFeat is not supported in dnn_feature_columns")

        sparse_embedding_list = [embedding_dict[feat.embedding_name](
            X[:, self.feature_index[feat.name][0]:self.feature_index[feat.name][1]].long()) for
            feat in sparse_feature_columns]

        varlen_sparse_embedding_list = get_varlen_pooling_list(self.embedding_dict, X, self.feature_index,
                                                               varlen_sparse_feature_columns, self.device)

        dense_value_list = [X[:, self.feature_index[feat.name][0]:self.feature_index[feat.name][1]] for feat in
                            dense_feature_columns]

        return sparse_embedding_list + varlen_sparse_embedding_list, dense_value_list

    def compute_input_dim(self, feature_columns, include_sparse=True, include_dense=True, feature_group=False):
        sparse_feature_columns = list(
            filter(lambda x: isinstance(x, (SparseFeat, VarLenSparseFeat)), feature_columns)) if len(
            feature_columns) else []
        dense_feature_columns = list(
            filter(lambda x: isinstance(x, DenseFeat), feature_columns)) if len(feature_columns) else []

        dense_input_dim = sum(
            map(lambda x: x.dimension, dense_feature_columns))
        if feature_group:
            sparse_input_dim = len(sparse_feature_columns)
        else:
            sparse_input_dim = sum(
                feat.embedding_dim for feat in sparse_feature_columns)
        input_dim = 0
        if include_sparse:
            input_dim += sparse_input_dim
        if include_dense:
            input_dim += dense_input_dim
        return input_dim

    def add_regularization_loss(self, weight_list, weight_decay, p=2):
        reg_loss = torch.zeros((1,), device=self.device)
        for w in weight_list:
            if isinstance(w, tuple):
                l2_reg = torch.norm(w[1], p=p, )
            else:
                l2_reg = torch.norm(w, p=p, )
            reg_loss = reg_loss + l2_reg
        reg_loss = weight_decay * reg_loss
        self.reg_loss = self.reg_loss + reg_loss

    def add_auxiliary_loss(self, aux_loss, alpha):
        self.aux_loss = aux_loss * alpha

    def compile(self, optimizer,
                loss=None,
                metrics=None,
                optimizer_sparse=None,
                optimizer_dense_lr=0.001,
                optimizer_sparse_lr=0.001,
                ):
        """
        :param optimizer: String (name of optimizer) or optimizer instance. See [optimizers](https://pytorch.org/docs/stable/optim.html).
        :param loss: String (name of objective function) or objective function. See [losses](https://pytorch.org/docs/stable/nn.functional.html#loss-functions).
        :param metrics: List of metrics to be evaluated by the model during training and testing. Typically you will use `metrics=['accuracy']`.
        """

        self.optim, self.optim_s = self._get_optim(
            optimizer, optimizer_sparse, optimizer_dense_lr, optimizer_sparse_lr)
        self.loss_func = self._get_loss_func(loss)
        self.metrics = self._get_metrics(metrics, False)

    def _get_optim(self, optimizer, optimizer_sparse, optimizer_dense_lr,
                   optimizer_sparse_lr):
        optim_s = None
        if optimizer_sparse is None:
            if isinstance(optimizer, str):
                if optimizer == "sgd":
                    optim = torch.optim.SGD(
                        self.parameters(), lr=optimizer_dense_lr)
                elif optimizer == "adam":
                    optim = torch.optim.Adam(
                        self.parameters(), lr=optimizer_dense_lr)  # 0.001
                elif optimizer == "adagrad":
                    optim = torch.optim.Adagrad(
                        self.parameters(), lr=optimizer_dense_lr)  # 0.01
                elif optimizer == "rmsprop":
                    optim = torch.optim.RMSprop(
                        self.parameters(), lr=optimizer_dense_lr)
                else:
                    raise NotImplementedError
            else:
                optim = optimizer
        else:
            def sparse_parameters(named_gen):
                for name, item in named_gen:
                    if 'embed' in name:
                        yield item

            def dense_parameters(named_gen):
                for name, item in named_gen:
                    if 'embed' not in name:
                        yield item

            if isinstance(optimizer, str):
                if optimizer == "sgd":
                    optim = torch.optim.SGD(dense_parameters(
                        self.named_parameters()), lr=optimizer_dense_lr)
                elif optimizer == "adam":
                    optim = torch.optim.Adam(dense_parameters(
                        self.named_parameters()), lr=optimizer_dense_lr)  # 0.001
                elif optimizer == "adagrad":
                    optim = torch.optim.Adagrad(
                        dense_parameters(self.named_parameters()), lr=optimizer_dense_lr)  # 0.01
                elif optimizer == "rmsprop":
                    optim = torch.optim.RMSprop(
                        dense_parameters(self.named_parameters()), lr=optimizer_dense_lr)
                else:
                    raise NotImplementedError
            else:
                optim = optimizer
            if isinstance(optimizer_sparse, str):
                if optimizer_sparse == "sgd":
                    optim_s = torch.optim.SGD(sparse_parameters(
                        self.named_parameters()), lr=optimizer_sparse_lr)
                elif optimizer_sparse == "adam":
                    optim_s = torch.optim.Adam(sparse_parameters(
                        self.named_parameters()), lr=optimizer_sparse_lr)  # 0.001
                elif optimizer_sparse == "adagrad":
                    optim_s = torch.optim.Adagrad(
                        sparse_parameters(self.named_parameters()), lr=optimizer_sparse_lr)  # 0.01
                elif optimizer_sparse == "radagrad":
                    optim_s = torch.optim.RAdagrad(
                        sparse_parameters(self.named_parameters()), lr=optimizer_sparse_lr, alpha = 0.9999)
                else:
                    raise NotImplementedError
            else:
                optim_s = optimizer_sparse

        return optim, optim_s

    def _get_loss_func(self, loss):
        if isinstance(loss, str):
            if loss == "binary_crossentropy":
                loss_func = F.binary_cross_entropy
            elif loss == "mse":
                loss_func = F.mse_loss
            elif loss == "mae":
                loss_func = F.l1_loss
            else:
                raise NotImplementedError
        else:
            loss_func = loss
        return loss_func

    def _log_loss(self, y_true, y_pred, eps=1e-7, normalize=True, sample_weight=None, labels=[0,1]):
        # change eps to improve calculation accuracy
        return log_loss(y_true,
                        y_pred,
                        eps,
                        normalize,
                        sample_weight,
                        labels)

    def _get_metrics(self, metrics, set_eps=False):
        metrics_ = {}
        if metrics:
            for metric in metrics:
                metric = metric.lower()
                if metric == "binary_crossentropy" or metric == "logloss":
                    if set_eps:
                        metrics_[metric] = self._log_loss
                    else:
                        metrics_[metric] = lambda y_true,y_pred: log_loss(y_true, y_pred, labels=[0,1])
                if metric == "auc":
                    metrics_[metric] = lambda y_true,y_pred: roc_auc_score(y_true, y_pred, labels=[0,1])
                if metric == "mse":
                    metrics_[metric] = mean_squared_error
                if metric == "accuracy" or metric == "acc":
                    metrics_[metric] = lambda y_true, y_pred: accuracy_score(
                        y_true, np.where(y_pred > 0.5, 1, 0))
        return metrics_

    @property
    def embedding_size(self, ):
        feature_columns = self.dnn_feature_columns
        sparse_feature_columns = list(
            filter(lambda x: isinstance(x, (SparseFeat, VarLenSparseFeat)), feature_columns)) if len(
            feature_columns) else []
        embedding_size_set = set(
            [feat.embedding_dim for feat in sparse_feature_columns])
        if len(embedding_size_set) > 1:
            raise ValueError(
                "embedding_dim of SparseFeat and VarlenSparseFeat must be same in this model!")
        return list(embedding_size_set)[0]
