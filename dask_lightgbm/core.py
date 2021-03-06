import logging
from collections import defaultdict

import lightgbm
import numpy as np
import pandas as pd
from lightgbm.basic import _safe_call, _LIB
from toolz import first, assoc
import dask.dataframe as dd
import dask.array as da

try:
    import sparse
    import scipy.sparse as ss
except ImportError:
    sparse = False
    ss = False

from dask import delayed
from dask.distributed import wait, default_client, get_worker

logger = logging.getLogger(__name__)


def parse_host_port(address):
    if '://' in address:
        address = address.rsplit('://', 1)[1]
    host, port = address.split(':')
    port = int(port)
    return host, port


def build_network_params(worker_addresses, local_worker_ip, local_listen_port, listen_time_out):
    addr_port_map = {addr: (local_listen_port + i) for i, addr in enumerate(worker_addresses)}
    params = {
        "machines": ",".join([parse_host_port(addr)[0] + ":" + str(port) for addr, port in addr_port_map.items()]),
        "local_listen_port": addr_port_map[local_worker_ip],
        "listen_time_out": listen_time_out,
        "num_machines": len(addr_port_map)
    }
    return params


def concat(L):
    if isinstance(L[0], np.ndarray):
        return np.concatenate(L, axis=0)
    elif isinstance(L[0], (pd.DataFrame, pd.Series)):
        return pd.concat(L, axis=0)
    elif ss and isinstance(L[0], ss.spmatrix):
        return ss.vstack(L, format='csr')
    elif sparse and isinstance(L[0], sparse.SparseArray):
        return sparse.concatenate(L, axis=0)
    else:
        raise TypeError("Data must be either numpy arrays or pandas dataframes. Got %s" % type(L[0]))


def _fit_local(params, model_factory, list_of_parts, worker_addresses, local_listen_port=12400, listen_time_out=120,
               **kwargs):
    network_params = build_network_params(worker_addresses, get_worker().address, local_listen_port,
                                          listen_time_out)
    params = {**params, **network_params}

    # Prepare data
    if len(list_of_parts[0]) == 3:
        data, labels, weight = zip(*list_of_parts)
        weight = concat(weight)
    else:
        data, labels = zip(*list_of_parts)
        weight = None

    data = concat(data)  # Concatenate many parts into one
    labels = concat(labels)

    try:
        classifier = model_factory(**params)
        classifier.fit(data, labels, sample_weight=weight)
    finally:
        _safe_call(_LIB.LGBM_NetworkFree())
    return classifier


def train(client, X, y, params, model_factory, sample_weight=None, **kwargs):
    data_parts = X.to_delayed()
    label_parts = y.to_delayed()
    if isinstance(data_parts, np.ndarray):
        assert data_parts.shape[1] == 1
        data_parts = data_parts.flatten().tolist()
    if isinstance(label_parts, np.ndarray):
        assert label_parts.ndim == 1 or label_parts.shape[1] == 1
        label_parts = label_parts.flatten().tolist()

    # Arrange parts into tuples.  This enforces co-locality
    if sample_weight is not None:
        sample_weight_parts = sample_weight.to_delayed()
        if isinstance(sample_weight_parts, np.ndarray):
            assert sample_weight_parts.ndim == 1 or sample_weight_parts.shape[1] == 1
            sample_weight_parts = sample_weight_parts.flatten().tolist()
        parts = list(map(delayed, zip(data_parts, label_parts, sample_weight_parts)))
    else:
        parts = list(map(delayed, zip(data_parts, label_parts)))

    parts = client.compute(parts)  # Start computation in the background
    wait(parts)
    # for part in parts:
    #     if part.status == 'error':
    #         part  # trigger error locally
    key_to_part_dict = dict([(part.key, part) for part in parts])
    who_has = client.who_has(parts)
    worker_map = defaultdict(list)
    for key, workers in who_has.items():
        worker_map[first(workers)].append(key_to_part_dict[key])
    ncores = client.ncores()  # Number of cores per worker
    params['tree_learner'] = "data"
    # Tell each worker to init the booster on the chunks/parts that it has locally
    futures_classifiers = [client.submit(_fit_local,
                                         model_factory=model_factory,
                                         params=assoc(params, 'num_threads', ncores[worker]),
                                         list_of_parts=list_of_parts,
                                         worker_addresses=list(worker_map.keys()),
                                         local_listen_port=params.get("local_listen_port", 12400),
                                         listen_time_out=params.get("listen_time_out", 120),
                                         **kwargs)
                           for worker, list_of_parts in worker_map.items()]

    results = client.gather(futures_classifiers)
    results = [v for v in results if v]
    return results[0]


def _predict_part(part, model, proba, **kwargs):

    if isinstance(part, pd.DataFrame):
        X = part.values
    else:
        X = part
    if proba:
        result = model.predict_proba(X, **kwargs)
    else:
        result = model.predict(X, **kwargs)

    if isinstance(part, pd.DataFrame):
        if proba:
            result = pd.DataFrame(result, index=part.index)
        else:
            result = pd.Series(result, index=part.index, name='predictions')
    return result


def predict(client, model, data, proba=False, dtype=np.float32, **kwargs):

    if isinstance(data, dd._Frame):
        result = data.map_partitions(_predict_part, model=model, proba=proba, **kwargs)
        result = result.values
    elif isinstance(data, da.Array):
        if proba:
            kwargs = dict(
                drop_axis=None,
                chunks=(data.chunks[0], (model.n_classes_,)),
            )
        else:
            kwargs = dict(drop_axis=1)

        result = data.map_blocks(_predict_part, model=model, proba=proba, dtype=dtype, **kwargs)

    return result


class LGBMClassifier(lightgbm.LGBMClassifier):

    def fit(self, X, y=None, sample_weight=None, client=None, **kwargs):
        if client is None:
            client = default_client()
        model_factory = lightgbm.LGBMClassifier
        params = self.get_params(True)

        model = train(client, X, y, params, model_factory, sample_weight, **kwargs)
        self.set_params(**model.get_params())
        self._Booster = model._Booster
        self._le = model._le
        self._classes = model._classes
        self._n_classes = model._n_classes
        self._n_features = model._n_features
        self._evals_result = model._evals_result
        self._best_iteration = model._best_iteration
        self._best_score = model._best_score

        return self
    fit.__doc__ = lightgbm.LGBMClassifier.fit.__doc__

    def _network_params(self):
        return {
            "machines": self.machines
        }

    def predict(self, X, client=None, **kwargs):
        if client is None:
            client = default_client()
        return predict(client, self.to_local(), X, dtype=self.classes_[0].dtype, **kwargs)
    predict.__doc__ = lightgbm.LGBMClassifier.predict.__doc__

    def predict_proba(self, X, client=None, **kwargs):
        if client is None:
            client = default_client()
        return predict(client, self.to_local(), X, proba=True, dtype=self.classes_[0].dtype, **kwargs)
    predict_proba.__doc__ = lightgbm.LGBMClassifier.predict_proba.__doc__

    def to_local(self):
        model = lightgbm.LGBMClassifier(**self.get_params())
        model._Booster = self._Booster
        model._le = self._le
        model._classes = self._classes
        model._n_classes = self._n_classes
        model._n_features = self._n_features
        model._evals_result = self._evals_result
        model._best_iteration = self._best_iteration
        model._best_score = self._best_score

        return model
