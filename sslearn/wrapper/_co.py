from collections import defaultdict
from sklearn.base import ClassifierMixin, BaseEstimator
from sklearn.naive_bayes import GaussianNB
from sklearn.tree import DecisionTreeClassifier
from sklearn.ensemble import BaggingClassifier
import numpy as np
from sklearn.utils.validation import check_is_fitted
from sklearn.utils import check_random_state, resample
from sklearn.exceptions import NotFittedError, ConvergenceWarning
from sklearn.base import clone as skclone
import math
from abc import abstractmethod
from sklearn.multiclass import LabelBinarizer
from sklearn.feature_selection import mutual_info_classif
from ..utils import (
    calculate_prior_probability,
    choice_with_proportion,
    confidence_interval,
    safe_division,
)
import sys
from sklearn.neighbors import KNeighborsClassifier
from sklearn.discriminant_analysis import softmax
from sklearn.preprocessing import OneHotEncoder, LabelEncoder
import warnings
from ..base import Ensemble, get_dataset
from sslearn.utils import check_n_jobs
from joblib import Parallel, delayed
import scipy.stats as st 
try:
    from loguru import logger as log
except ImportError:
    print("No logger available. Install loguru.", file=sys.stderr)


class _BaseCoTraining(BaseEstimator, ClassifierMixin, Ensemble):
    @abstractmethod
    def fit(self, X, y, **kwards):
        pass

    def predict_proba(self, X, **kwards):
        """Predict probability for each possible outcome.

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            Array representing the data.
        Returns
        -------
        ndarray of shape (n_samples, n_features)
            Array with prediction probabilities.
        """
        if "h_" in dir(self):
            ys = []
            ys = [h.predict_proba(X[:, c], **kwards)
                  for h, c in zip(self.h_, self.columns_)]
            y = sum(ys) / len(ys)
            return y
        else:
            raise NotFittedError("Classifier not fitted")


# Done and tested
class DemocraticCoLearning(_BaseCoTraining):
    def __init__(
        self,
        base_estimator=[
            DecisionTreeClassifier(),
            GaussianNB(),
            KNeighborsClassifier(n_neighbors=3),
        ],
        n_estimators=3,
        expand_only_mislabeled=True,
        confidence_method="bernoulli",
        alpha=0.95,
        random_state=None,
        logging=False,
        log_name="",
        save_dict=None
    ):
        """
        Y. Zhou and S. Goldman, "Democratic co-learning,"
        16th IEEE International Conference on Tools with Artificial Intelligence,
        2004, pp. 594-602, doi: 10.1109/ICTAI.2004.48.

        Parameters
        ----------
        base_estimator : {ClassifierMixin, list}, optional
            An estimator object implementing fit and predict_proba or a list of ClassifierMixin, by default DecisionTreeClassifier()
        n_estimators : int, optional
            number of base_estimators to use. None if base_estimator is a list, by default 3

        Raises
        ------
        AttributeError
            If n_estimators is None and base_estimator is not a list
        """

        if isinstance(base_estimator, ClassifierMixin) and n_estimators is not None:
            estimators = list()
            random_available = True
            rand = check_random_state(random_state)
            if "random_state" not in dir(base_estimator):
                warnings.warn(
                    "The classifier will not be able to converge correctly, there is not enough diversity among the estimators (learners should be different).",
                    ConvergenceWarning,
                )
                random_available = False
            for i in range(n_estimators):
                estimators.append(skclone(base_estimator))
                if random_available:
                    estimators[i].random_state = rand.randint(0, 1e5)
            self.base_estimator = estimators

        elif isinstance(base_estimator, list):
            self.base_estimator = base_estimator
        else:
            raise AttributeError(
                "If `n_estimators` is None then `base_estimator` must be a `list`."
            )
        self.n_estimators = len(self.base_estimator)
        self.one_hot = OneHotEncoder(sparse=False)
        self.expand_only_mislabeled = expand_only_mislabeled

        self.confidence_method = confidence_method
        self.alpha = alpha
        self.random_state = random_state
        self.logging = logging
        self.log_name = log_name
        if save_dict is not None:
            save_dict[log_name] = list()
        self.save_dict = save_dict

    def __ponderate_y(self, predictions, weights):
        y_complete = np.sum(
            [
                self.one_hot.transform(p.reshape(-1, 1)) * wi
                for p, wi in zip(predictions, weights)
            ],
            0
        )
        y_zeros = np.zeros(y_complete.shape)
        y_zeros[np.arange(y_complete.shape[0]), y_complete.argmax(1)] = 1

        return self.one_hot.inverse_transform(y_zeros).flatten()

    def __calcule_last_confidences(self, X, y):
        """Calculate the confidence of each learner

        Parameters
        ----------
        X : array-like
            Set of instances
        y : array-like
            Set of classes for each instance
        """
        w = []
        # for H in self.h_:
        #     li, hi = confidence_interval(X, H, y, self.confidence_method, self.alpha)
        #     w.append((li + hi) / 2)
        conf_method, alpha = (self.confidence_method, self.alpha)
        w = [sum(confidence_interval(X, H, y, conf_method, alpha)) / 2
             for H in self.h_]
        self.confidences_ = w

    def fit(self, X, y, estimator_kwards=None):
        """Fit Democratic-Co classifier

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            The training input samples.
        y : array-like of shape (n_samples,)
            The target values (class labels), -1 if unlabel.
        estimator_kwards : {list, dict}, optional
            list of kwards for each estimator or kwards for all estimators, by default None

        Returns
        -------
        self
            fitted classifier
        """
        if self.logging:
            log.info("Democratic for {}", self.log_name)
            log.info("Fitting DemocraticCo with {} estimators {}", self.n_estimators, self.base_estimator)
        X_label, y_label, X_unlabel = get_dataset(X, y)

        self.one_hot.fit(y_label.reshape(-1, 1))

        L = [X_label] * self.n_estimators
        Ly = [y_label] * self.n_estimators
        # This variable prevents duplicate instances.
        L_added = [np.zeros(X_unlabel.shape[0]).astype(bool)] * self.n_estimators
        e = [0] * self.n_estimators

        if estimator_kwards is None:
            estimator_kwards = [{}] * self.n_estimators

        changed = True
        iteration = 0
        while changed:
            changed = False
            iteration_dict = {}
            iteration += 1
            if self.logging:
                log.log("EVO", "Iteration: {}", iteration)

            for i in range(self.n_estimators):
                self.base_estimator[i].fit(L[i], Ly[i], **estimator_kwards[i])

            # Majority Vote
            if self.logging:
                log.info("Calculate majority class")
            predictions = [H.predict(X_unlabel) for H in self.base_estimator]
            majority_class = st.mode(np.array(predictions), axis=0)[
                0
            ].flatten()  # K in pseudocode

            L_ = [[]] * self.n_estimators
            Ly_ = [[]] * self.n_estimators

            # Calculate confidence interval
            conf_interval = [
                confidence_interval(
                    X_label, H, y_label, self.confidence_method, self.alpha, self.logging
                )
                for H in self.base_estimator
            ]

            weights = [(li + hi) / 2 for (li, hi) in conf_interval]
            iteration_dict["weights"] = {"cl" + str(i): (l, h, w) for i, ((l, h), w) in enumerate(zip(conf_interval, weights))}
            # Ponderate vote
            ponderate_class = self.__ponderate_y(predictions, weights)

            # If `ponderate_class` is equal as `majority_class` then
            # the sum of classifier's weights of max voted class
            # is greater than the max of sum of classifier's weights
            # from another classes.

            candidates = ponderate_class == majority_class
            candidates_bool = list()

            if not self.expand_only_mislabeled:
                all_same_list = list()
                for i in range(1, self.n_estimators):
                    all_same_list.append(predictions[i] == predictions[i - 1])
                all_same = np.logical_and(*all_same_list)
            new_instances = []
            for i in range(self.n_estimators):

                mispredictions = predictions[i] != ponderate_class
                # An instance from U are added to Li' only if:
                #   It is a misprediction for i
                #   It is a candidate (ponderate_class are same majority_class)
                #   It hasn't been added yet in Li

                candidates_temp = np.logical_and(mispredictions, candidates)

                if not self.expand_only_mislabeled:
                    candidates_temp = np.logical_or(candidates_temp, all_same)

                to_add = np.logical_and(
                    np.logical_not(L_added[i]), candidates_temp
                )

                candidates_bool.append(to_add)
                L_[i] = X_unlabel[to_add, :]
                Ly_[i] = ponderate_class[to_add]

                if self.logging:
                    log.log("EVO", "New instances for classifier {}: {}", i, L_[i].shape[0])
                new_instances.append(L_[i].shape[0])
            iteration_dict["new_instances"] = new_instances

            new_conf_interval = [
                confidence_interval(L[i], H, Ly[i], self.confidence_method, self.alpha)
                for i, H in enumerate(self.base_estimator)
            ]
            e_factor = (
                1 - sum([l_ for l_, _ in new_conf_interval]) / self.n_estimators
            )
            evolution = {}
            for i, _ in enumerate(self.base_estimator):
                if len(L_[i]) > 0:

                    qi = len(L[i]) * ((1 - 2 * (e[i] / len(L[i]))) ** 2)
                    e_i = e_factor * len(L_[i])
                    # |Li|+|L'i| == |Li U L'i| because of to_add
                    q_i = (len(L[i]) + len(L_[i])) * (
                        1 - 2 * (e[i] + e_i) / (len(L[i]) + len(L_[i]))
                    ) ** 2
                    if self.logging:
                        log.log("EVO", "Values: qi:{:.2f}, q'i: {:.2f}, e'i:{:.2f}", qi, q_i, e_i)
                    evolution["cl" + str(i)] = [qi, q_i, e_i]
                    if q_i <= qi:
                        continue
                    L_added[i] = np.logical_or(L_added[i], candidates_bool[i])
                    L[i] = np.concatenate((L[i], np.array(L_[i])))
                    Ly[i] = np.concatenate((Ly[i], np.array(Ly_[i])))

                    e[i] = e[i] + e_i
                    if self.logging:
                        log.log("EVO", "New ei: {:.2f}. L{} will be increased", e[i], i)
                    evolution["cl" + str(i)].append(e[i])
                    changed = True
                else:
                    evolution["cl" + str(i)] = "No new instances"
            iteration_dict["evolution"] = evolution
            self.save_dict[self.log_name].append(iteration_dict)
        log.info("Finished fitting")

        self.h_ = self.base_estimator
        self.classes_ = self.h_[0].classes_
        self.__calcule_last_confidences(X_label, y_label)

        # Ignore hypothesis
        self.h_ = [H for w, H in zip(self.confidences_, self.h_) if w > 0.5]
        self.confidences_ = [w for w in self.confidences_ if w > 0.5]

        self.columns_ = [list(range(X.shape[1]))] * self.n_estimators

        return self

    def __combine_probabilities(self, X):
        # TODO: Vectorize

        # CGO the vectorization assuming models with w<0.5 have been already removed:
        # n_instances = X.shape[0]  # uppercase X as it will be an np.array
        # sizes = np.zeros((n_instances,), dtype=int)
        # C = np.zeros((n_instances,), dtype=float)
        # Cavg = np.zeros((n_instances,), dtype=float)
        # for w, H in zip(self.confidences_, self.h_):
        #     cj = H.predict(X.reshape(1, -1))
        #     C[cj] += w
        #     sizes[cj] += 1
        # Cavg[sizes == 0] = 0.5  # «voting power» of 0.5 for small groups
        # ne = (sizes != 0)  # non empty groups
        # Cavg[ne] = (sizes[ne] + 0.5) / (sizes[ne] + 1) * C[ne] / sizes[ne]
        # return softmax(Cavg)[0]

        groups = defaultdict(list)

        for w, H in zip(self.confidences_, self.h_):
            if w > 0.5:
                cj = H.predict(X.reshape(1, -1))
                groups[cj[0]].append(w)

        C_G_j = list()
        for c in self.classes_:
            size = len(groups[c])
            if size == 0:
                cgj = 0.5
            else:
                cgj = ((size + 0.5) / (size + 1)) * (sum(groups[c]) / size)
            C_G_j.append(cgj)
        return softmax(np.array(C_G_j).reshape(1, -1))[0]

    def predict_proba(self, X):
        """Predict probability for each possible outcome.

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            Array representing the data.
        Returns
        -------
        ndarray of shape (n_samples, n_features)
            Array with prediction probabilities.

        Need to vectorized
        """
        if "h_" in dir(self):
            if len(X) == 1:
                X = [X]
            return np.apply_along_axis(self.__combine_probabilities, 1, X)
        else:
            raise NotFittedError("Classifier not fitted")


# Done and tested
class CoTraining(_BaseCoTraining):
    """
    Implementation based on https://github.com/jjrob13/sklearn_cotraining

    Avrim Blum and Tom Mitchell. 1998.
    Combining labeled and unlabeled data with co-training.
    In Proceedings of the eleventh annual conference on Computational learning theory (COLT' 98).
    Association for Computing Machinery, New York, NY, USA, 92–100.
    DOI:https://doi.org/10.1145/279943.279962
    """

    def __init__(
        self,
        base_estimator=DecisionTreeClassifier(),
        second_base_estimator=None,
        max_iterations=30,
        poolsize=75,
        positives=-1,
        negatives=-1,
        random_state=None,
    ):
        """Create a CoTraining classifier

        Parameters
        ----------
        base_estimator : ClassifierMixin, optional
            The classifier that will be used in the cotraining algorithm on the feature set, by default DecisionTreeClassifier()
        second_base_estimator : ClassifierMixin, optional
            The classifier that will be used in the cotraining algorithm on another feature set, if none are a clone of base_estimator, by default None
        max_iterations : int, optional
            The number of iterations, by default 30
        poolsize : int, optional
            The size of the pool of unlabeled samples from which the classifier can choose, by default 75
        positives : int, optional
            The number of positive examples that will be 'labeled' by each classifier during each iteration
            The default is the is determined by the smallest integer ratio of positive to negative samples in L, by default -1
        negatives : int, optional
            The number of negative examples that will be 'labeled' by each classifier during each iteration
            The default is the is determined by the smallest integer ratio of positive to negative samples in L, by default -1
        random_state : int, RandomState instance, optional
            controls the randomness of the estimator, by default None

        Raises
        ------
        ValueError
            Current implementation supports either both positives and negatives being specified, or neither
        """
        assert isinstance(
            base_estimator, ClassifierMixin
        ), "This method only support classification"

        self.base_estimator = base_estimator
        self.second_base_estimator = second_base_estimator

        self.max_iterations = max_iterations
        self.poolsize = poolsize
        self.random_state = random_state

        if (positives == -1 and negatives != -1) or (
            positives != -1 and negatives == -1
        ):
            raise ValueError(
                "Current implementation supports either both positives and negatives being specified, or neither"
            )

        self.positives = positives
        self.negatives = negatives

    def fit(self, X, y, X2=None, features: list = None, **kwards):
        """
        Build a CoTraining classifier from the training set.

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            Array representing the data.
        y : array-like of shape (n_samples,)
            The target values (class labels), -1 if unlabel.
        X2 : {array-like, sparse matrix} of shape (n_samples, n_features), optional
            Array representing the data from another view, not compatible with `features`, by default None
        features : {list, tuple}, optional
            list or tuple of two arrays with `feature` index for each subspace view, not compatible with `X2`, by default None

        Returns
        -------
        self: CoTraining
            Fitted estimator.
        """
        rs = check_random_state(self.random_state)

        self.h = [skclone(self.base_estimator)]
        if self.second_base_estimator is not None:
            self.h.append(skclone(self.second_base_estimator))
        else:
            self.h.append(skclone(self.base_estimator))

        y = y.copy()
        X = X.copy()
        X = np.asarray(X)
        y = np.asarray(y)
        assert not (
            X2 is not None and features is not None
        ), "The list of features and x2 cannot be defined at the same time"
        X1 = X
        if X2 is None and features is None:
            X2 = X.copy()
            self.columns_ = [list(range(X.shape[1]))] * 2
        elif X2 is not None:
            X2 = np.asarray(X2)
        elif features is not None:
            X1 = X[:, features[0]]
            X2 = X[:, features[1]]
            self.columns_ = features

        if self.positives == -1 and self.negatives == -1:

            num_pos = sum(1 for y_i in y if y_i == 1)
            num_neg = sum(1 for y_i in y if y_i == 0)

            n_p_ratio = num_neg / float(num_pos)

            if n_p_ratio > 1:
                self.positives = 1
                self.negatives = round(self.positives * n_p_ratio)

            else:
                self.negatives = 1
                self.positives = round(self.negatives / n_p_ratio)

        assert (
            self.positives > 0
            and self.negatives > 0
            and self.max_iterations > 0
            and self.poolsize > 0
        ), "Parameters are inconsistent"

        # Set of unlabeled samples
        U = [i for i, y_i in enumerate(y) if y_i == -1]
        rs.shuffle(U)

        U_ = U[-min(len(U), self.poolsize) :]
        # remove the samples in U_ from U
        U = U[: -len(U_)]

        L = [i for i, y_i in enumerate(y) if y_i != -1]

        y = y.reshape((y.shape[0], 1))

        self.label_binarize = LabelBinarizer().fit(y[L])
        y[L] = self.label_binarize.transform(y[L])

        it = 0
        while it != self.max_iterations and U:
            it += 1

            self.h[0].fit(X1[L], y[L], **kwards)
            self.h[1].fit(X2[L], y[L], **kwards)

            if len(self.h[0].classes_) > 2:
                raise Exception("CoTraining does not support multiclass")

            y1_prob = self.h[0].predict_proba(X1[U_])
            y2_prob = self.h[1].predict_proba(X2[U_])

            n, p = [], []

            for i in (y1_prob[:, 0].argsort())[-self.negatives :]:
                if y1_prob[i, 0] > 0.5:
                    n.append(i)
            for i in (y1_prob[:, 1].argsort())[-self.positives :]:
                if y1_prob[i, 1] > 0.5:
                    p.append(i)

            for i in (y2_prob[:, 0].argsort())[-self.negatives :]:
                if y2_prob[i, 0] > 0.5:
                    n.append(i)
            for i in (y2_prob[:, 1].argsort())[-self.positives :]:
                if y2_prob[i, 1] > 0.5:
                    p.append(i)

            y[[U_[x] for x in p]] = 1
            y[[U_[x] for x in n]] = 0

            L.extend([U_[x] for x in p])
            L.extend([U_[x] for x in n])

            U_ = [elem for elem in U_ if not (elem in p or elem in n)]

            add_counter = 0  # number we have added from U to U_
            num_to_add = len(p) + len(n)
            while add_counter != num_to_add and U:
                add_counter += 1
                U_.append(U.pop())

        self.h[0].fit(X1[L], y[L], **kwards)
        self.h[1].fit(X2[L], y[L], **kwards)
        self.h_ = self.h
        self.classes_ = self.h_[0].classes_

        return self

    def predict_proba(self, X, X2=None, **kwards):
        """Predict probability for each possible outcome.

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            Array representing the data.
        X2 : {array-like, sparse matrix} of shape (n_samples, n_features), optional
            Array representing the data from another view, by default None
        Returns
        -------
        ndarray of shape (n_samples, n_features)
            Array with prediction probabilities.
        """
        if "columns_" in dir(self):
            return super().predict_proba(X, **kwards)
        elif "h_" in dir(self):
            ys = []
            ys.append(self.h_[0].predict_proba(X, **kwards))
            ys.append(self.h_[1].predict_proba(X2, **kwards))
            y = sum(ys) / len(ys)
            return y
        else:
            raise NotFittedError("Classifier not fitted")

    def predict(self, X, X2=None, **kwards):
        """Predict the classes of X.
        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            Array representing the data.
        X2 : {array-like, sparse matrix} of shape (n_samples, n_features), optional
            Array representing the data from another view, by default None

        Returns
        -------
        y : ndarray of shape (n_samples,)
            Array with predicted labels.
        """
        if "columns_" in dir(self):
            result = super().predict(X, **kwards)
        else:
            predicted_probabilitiy = self.predict_proba(X, X2, **kwards)
            result = self.classes_.take(
                (np.argmax(predicted_probabilitiy, axis=1)), axis=0
            )
        return self.label_binarize.inverse_transform(result)

# Done and tested
class Rasco(_BaseCoTraining):
    def __init__(
        self,
        base_estimator=DecisionTreeClassifier(),
        max_iterations=10,
        n_estimators=30,
        incremental=True,
        batch_size=None,
        subspace_size=None,
        random_state=None,
        n_jobs=None,
    ):
        """
        Co-Training based on random subspaces

        Wang, J., Luo, S. W., & Zeng, X. H. (2008, June).
        A random subspace method for co-training.
        In <i>2008 IEEE International Joint Conference on Neural Networks</i>
        (IEEE World Congress on Computational Intelligence)
        (pp. 195-200). IEEE.

        Parameters
        ----------
        base_estimator : ClassifierMixin, optional
            An estimator object implementing fit and predict_proba, by default DecisionTreeClassifier()
        max_iterations : int, optional
            Maximum number of iterations allowed. Should be greater than or equal to 0.
            If is -1 then will be infinite iterations until U be empty, by default 10
        n_estimators : int, optional
            The number of base estimators in the ensemble., by default 30
        incremental : bool, optional
            If true then it will add the most relevant instance for each class from U in enlarged L,
            else will be select from U the "batch_size" most confident instances., by default True
        batch_size : int, optional
            If "incremental" is false it is the number of instances to add to enlarged L.
            If it is None then will be the size of L., by default None
        subspace_size : int, optional
            The number of features for each subspace. If it is None will be the half of the features size., by default None
        random_state : int, RandomState instance, optional
            controls the randomness of the estimator, by default None
        """
        assert isinstance(
            base_estimator, ClassifierMixin
        ), "This method only support classification"
        self.base_estimator = base_estimator  # C in paper
        self.max_iterations = max_iterations  # J in paper
        self.n_estimators = n_estimators  # K in paper
        self.subspace_size = subspace_size  # m in paper
        self.incremental = incremental
        self.batch_size = batch_size
        self.n_jobs = check_n_jobs(n_jobs)

        self.random_state = random_state

    def _generate_random_subspaces(self, X, y=None, random_state=None):
        """Generate the random subspaces

        Parameters
        ----------
        X : array like
            Labeled dataset
        y : array like, optional
            Target for each X, not needed on Rasco, by default None

        Returns
        -------
        list
            List of index of features
        """
        random_state = check_random_state(random_state)
        features = list(range(X.shape[1]))
        idxs = []
        for _ in range(self.n_estimators):
            idxs.append(random_state.permutation(features)[: self.subspace_size])
        return idxs

    def __fit_estimator(self, X, y, **kwards):
        return skclone(self.base_estimator).fit(X, y, **kwards)

    def fit(self, X, y, **kwards):
        """Build a Rasco classifier from the training set (X, y).

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            The training input samples.
        y : array-like of shape (n_samples,)
            The target values (class labels), -1 if unlabel.

        Returns
        -------
        self: Rasco
            Fitted estimator.
        """
        X_label, y_label, X_unlabel = get_dataset(X, y)

        random_state = check_random_state(self.random_state)

        if not self.incremental and self.batch_size is None:
            self.batch_size = X_label.shape[0]

        if self.subspace_size is None:
            self.subspace_size = int(X.shape[1] / 2)
        idxs = self._generate_random_subspaces(X_label, y_label, random_state)

        cfs = Parallel(n_jobs=self.n_jobs)(
            delayed(self.__fit_estimator)(X_label[:, idxs[i]], y_label, **kwards)
            for i in range(self.n_estimators)
        )

        it = 0
        while True:
            if (self.max_iterations != -1 and it >= self.max_iterations) or len(
                X_unlabel
            ) == 0:
                break

            raw_predicions = []
            for i in range(self.n_estimators):
                rp = cfs[i].predict_proba(X_unlabel[:, idxs[i]])
                raw_predicions.append(rp)
            raw_predicions = sum(raw_predicions) / self.n_estimators
            predictions = np.max(raw_predicions, axis=1)
            class_predicted = np.argmax(raw_predicions, axis=1)
            pseudoy = np.array(list(map(lambda x: cfs[0].classes_[x], class_predicted)))

            Lj = []
            yj = []

            sorted_ = np.argsort(predictions)
            if self.incremental:
                # One of each class
                for class_ in cfs[0].classes_:
                    try:
                        Lj.append(sorted_[pseudoy == class_][-1])
                        yj.append(class_)
                    except IndexError:
                        warnings.warn(
                            "RASCO convergence warning, the class "
                            + str(class_)
                            + " not predicted",
                            ConvergenceWarning,
                        )
                Lj = np.array(Lj)
            else:
                Lj = sorted_[-self.batch_size :]
                yj = pseudoy[Lj]

            X_label = np.append(X_label, X_unlabel[Lj, :], axis=0)
            y_label = np.append(y_label, yj)
            X_unlabel = np.delete(X_unlabel, Lj, axis=0)

            cfs = Parallel(n_jobs=self.n_jobs)(
                delayed(self.__fit_estimator)(X_label[:, idxs[i]], y_label, **kwards)
                for i in range(self.n_estimators)
            )

            it += 1

        self.h_ = cfs
        self.classes_ = self.h_[0].classes_
        self.columns_ = idxs

        return self

# Done and tested
class RelRasco(Rasco):
    def __init__(
        self,
        base_estimator=DecisionTreeClassifier(),
        max_iterations=10,
        n_estimators=30,
        incremental=True,
        batch_size=None,
        subspace_size=None,
        random_state=None,
        n_jobs=None,
    ):
        """Co-Training with relevant random subspaces

        Yaslan, Y., & Cataltepe, Z. (2010).
        Co-training with relevant random subspaces.
        <i>Neurocomputing</i>, 73(10-12), 1652-1661.


        Parameters
        ----------
        base_estimator : ClassifierMixin, optional
            An estimator object implementing fit and predict_proba, by default DecisionTreeClassifier()
        max_iterations : int, optional
            Maximum number of iterations allowed. Should be greater than or equal to 0.
            If is -1 then will be infinite iterations until U be empty, by default 10
        n_estimators : int, optional
            The number of base estimators in the ensemble., by default 30
        incremental : bool, optional
            If true then it will add the most relevant instance for each class from U in enlarged L,
            else will be select from U the "batch_size" most confident instances., by default True
        batch_size : int, optional
            If "incremental" is false it is the number of instances to add to enlarged L.
            If it is None then will be the size of L., by default None
        subspace_size : int, optional
            The number of features for each subspace. If it is None will be the half of the features size., by default None
        random_state : int, RandomState instance, optional
            controls the randomness of the estimator, by default None
        n_jobs : int, optional
            The number of jobs to run in parallel. -1 means using all processors., by default None
        """
        super().__init__(
            base_estimator,
            max_iterations,
            n_estimators,
            incremental,
            batch_size,
            subspace_size,
            random_state,
            n_jobs,
        )

    def _generate_random_subspaces(self, X, y, random_state=None):
        """Generate the relevant random subspcaes

        Parameters
        ----------
        X : array like
            Labeled dataset
        y : array like, optional
            Target for each X, only needed on Rel-Rasco, by default None

        Returns
        -------
        list
            List of index of features
        """
        random_state = check_random_state(random_state)
        relevance = mutual_info_classif(X, y, random_state=random_state)
        idxs = []
        for _ in range(self.n_estimators):
            subspace = []
            for __ in range(self.subspace_size):
                f1 = random_state.randint(0, X.shape[1])
                f2 = random_state.randint(0, X.shape[1])
                if relevance[f1] > relevance[f2]:
                    subspace.append(f1)
                else:
                    subspace.append(f2)
            idxs.append(subspace)
        return idxs


# class RotRelRasco(RelRasco):
#     def __init__(
#         self,
#         base_estimator=DecisionTreeClassifier(),
#         group_weight=0.5,
#         pca=PCA(),
#         pre_rotation=False,
#         max_iterations=10,
#         n_estimators=30,
#         incremental=True,
#         batch_size=None,
#         subspace_size=None,
#         random_state=None,
#     ):
#         """
#         Parameters
#         ----------
#         base_estimator : ClassifierMixin, optional
#             An estimator object implementing fit and predict_proba, by default DecisionTreeClassifier()
#         max_iterations : int, optional
#             Maximum number of iterations allowed. Should be greater than or equal to 0.
#             If is -1 then will be infinite iterations until U be empty, by default 10
#         n_estimators : int, optional
#             The number of base estimators in the ensemble., by default 30
#         incremental : bool, optional
#             If true then it will add the most relevant instance for each class from U in enlarged L,
#             else will be select from U the "batch_size" most confident instances., by default True
#         batch_size : int, optional
#             If "incremental" is false it is the number of instances to add to enlarged L.
#             If it is None then will be the size of L., by default None
#         subspace_size : int, optional
#             The number of features for each subspace. If it is None will be the half of the features size., by default None
#         random_state : int, RandomState instance, optional
#             controls the randomness of the estimator, by default None
#         """
#         super().__init__(
#             base_estimator,
#             max_iterations,
#             n_estimators,
#             incremental,
#             batch_size,
#             subspace_size,
#             random_state,
#         )
#         self.group_weight = group_weight
#         self.pca = pca
#         self.pre_rotation = pre_rotation

#     def fit(self, X, y, **kwards):
#         """Build a RotRelRasco classifier from the training set (X, y).

#         Parameters
#         ----------
#         X : {array-like, sparse matrix} of shape (n_samples, n_features)
#             The training input samples.
#         y : array-like of shape (n_samples,)
#             The target values (class labels), -1 if unlabel.

#         Returns
#         -------
#         self: Rasco
#             Fitted estimator.
#         """
#         self._rotations = dict()

#         random_state = check_random_state(self.random_state)

#         X_label, y_label, X_unlabel = get_dataset(X, y)

#         if not self.incremental and self.batch_size is None:
#             self.batch_size = X_label.shape[0]

#         if self.subspace_size is None:
#             self.subspace_size = int(X.shape[1] / 2)

#         if self.pre_rotation:
#             self._rotations = rot.Rotation(
#                 math.ceil(X.shape[1] / self.subspace_size),
#                 self.group_weight,
#                 skclone(self.pca),
#                 random_state=random_state,
#             )
#             X_unlabel = self._rotations.fit_transform(X_unlabel)
#             X_label = self._rotations.transform(X_label)

#         idxs = self._generate_random_subspaces(
#             X_label, y_label, random_state=random_state
#         )

#         if not self.pre_rotation:
#             for idx in idxs:
#                 self.__rotate(X_unlabel, idx, fit=True, random_state=random_state)

#         cfs = []

#         for i in range(self.n_estimators):
#             if self.pre_rotation:
#                 r = X_label[:, idxs[i]]
#             else:
#                 r = self.__rotate(X_label, idxs[i])

#             cfs.append(skclone(self.base_estimator).fit(r, y_label, **kwards))

#         it = 0
#         while True:
#             if (self.max_iterations != -1 and it >= self.max_iterations) or len(
#                 X_unlabel
#             ) == 0:
#                 break

#             raw_predicions = []
#             for i in range(self.n_estimators):
#                 if self.pre_rotation:
#                     r = X_unlabel[:, idxs[i]]
#                 else:
#                     r = self.__rotate(X_unlabel, idxs[i])
#                 rp = cfs[i].predict_proba(r)
#                 raw_predicions.append(rp)
#             raw_predicions = sum(raw_predicions) / self.n_estimators
#             predictions = np.max(raw_predicions, axis=1)
#             class_predicted = np.argmax(raw_predicions, axis=1)
#             pseudoy = np.array(list(map(lambda x: cfs[0].classes_[x], class_predicted)))

#             Lj = []
#             yj = []

#             sorted_ = np.argsort(predictions)
#             if self.incremental:
#                 # One of each class
#                 for class_ in cfs[0].classes_:
#                     try:
#                         Lj.append(sorted_[pseudoy == class_][-1])
#                         yj.append(class_)
#                     except IndexError:
#                         warnings.warn(
#                             "RASCO convergence warning, the class "
#                             + str(class_)
#                             + " not predicted",
#                             ConvergenceWarning,
#                         )

#                 Lj = np.array(Lj)
#             else:
#                 Lj = sorted_[-self.batch_size :]
#                 yj = pseudoy[Lj]

#             X_label = np.append(X_label, X_unlabel[Lj, :], axis=0)
#             y_label = np.append(y_label, yj)
#             X_unlabel = np.delete(X_unlabel, Lj, axis=0)

#             for i in range(self.n_estimators):
#                 if self.pre_rotation:
#                     r = X_label[:, idxs[i]]
#                 else:
#                     r = self.__rotate(X_label, idxs[i])
#                 cfs[i].fit(r, y_label, **kwards)

#             it += 1

#         self.h_ = cfs
#         self.classes_ = self.h_[0].classes_
#         self.columns_ = idxs

#         return self

#     def __rotate(self, X, idx, fit=False, random_state=None):
#         if fit:
#             rt = rot.Rotation(
#                 len(idx),
#                 self.group_weight,
#                 skclone(self.pca),
#                 random_state=random_state,
#             )
#             self._rotations[tuple(idx)] = rt
#             rt.fit(X[:, idx])
#         else:
#             rt = self._rotations[tuple(idx)]
#         X_rotated = rt.transform(X[:, idx])
#         return X_rotated

#     def predict_proba(self, X, **kwards):
#         """Predict probability for each possible outcome.

#         Parameters
#         ----------
#         X : {array-like, sparse matrix} of shape (n_samples, n_features)
#             Array representing the data.
#         Returns
#         -------
#         ndarray of shape (n_samples, n_features)
#             Array with prediction probabilities.
#         """
#         if "h_" in dir(self):
#             ys = []
#             if self.pre_rotation:
#                 X = self._rotations.transform(X)
#             for i in range(len(self.h_)):
#                 if self.pre_rotation:
#                     r = X[:, self.columns_[i]]
#                 else:
#                     r = self.__rotate(X, self.columns_[i])
#                 ys.append(self.h_[i].predict_proba(r, **kwards))
#             y = sum(ys) / len(ys)
#             return y
#         else:
#             raise NotFittedError("Classifier not fitted")


# Done and tested
class TriTraining(_BaseCoTraining):
    def __init__(
        self,
        base_estimator=DecisionTreeClassifier(),
        n_samples=None,
        random_state=None,
        n_jobs=None
    ):
        """TriTraining
        Zhi-Hua Zhou and Ming Li,
        "Tri-training: exploiting unlabeled data using three classifiers,"
        in <i>IEEE Transactions on Knowledge and Data Engineering</i>,
        vol. 17, no. 11, pp. 1529-1541, Nov. 2005,
        doi: 10.1109/TKDE.2005.186.
        Parameters
        ----------
        base_estimator : ClassifierMixin, optional
            An estimator object implementing fit and predict_proba, by default DecisionTreeClassifier()
        n_samples : int, optional
            Number of samples to generate.
            If left to None this is automatically set to the first dimension of the arrays., by default None
        random_state : int, RandomState instance, optional
            controls the randomness of the estimator, by default None
        """
        self.base_estimator = base_estimator
        self.n_samples = n_samples
        self._N_LEARNER = 3
        self._epsilon = sys.sys.float_info.epsilon
        self.random_state = random_state
        self.n_jobs = check_n_jobs(n_jobs)

    @staticmethod
    def _measure_error(X, y, h1: ClassifierMixin, h2: ClassifierMixin, epsilon=sys.float_info.epsilon):
        """Calculate the error between two hypothesis
        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            The training labeled input samples.
        y : array-like of shape (n_samples,)
            The target values (class labels).
        h1 : ClassifierMixin
            First hypothesis
        h2 : ClassifierMixin
            Second hypothesis
        Returns
        -------
        float
            Divition of the number of labeled examples on which both h1 and h2 make incorrect classification,
            by the number of labeled examples on which the classification made by h1 is the same as that made by h2.
        """
        y1 = h1.predict(X)
        y2 = h2.predict(X)

        error = np.count_nonzero(np.logical_and(y1 == y2, y2 != y))
        coincidence = np.count_nonzero(y1 == y2)
        return safe_division(error, coincidence, epsilon)

    @staticmethod
    def _another_hs(hs, index):
        """Get the another hypothesis
        Parameters
        ----------
        hs : list
            hypotheses collection
        index : int
            base hypothesis  index
        Returns
        -------
        list
        """
        another_hs = []
        for i in range(len(hs)):
            if i != index:
                another_hs.append(hs[i])
        return another_hs

    @staticmethod
    def _subsample(L, s, random_state=None):
        """Randomly removes |L| - s number of examples from L
        Parameters
        ----------
        L : tuple of array-like
            Collection pseudo-labeled candidates and its labels
        s : int
            Equation 10 in paper
        random_state : int, RandomState instance, optional
            controls the randomness of the estimator, by default None
        Returns
        -------
        tuple
            Collection of pseudo-labeled selected for enlarged labeled examples.
        """
        to_remove = len(L[0]) - s
        select = len(L[0]) - to_remove

        return resample(*L, replace=False, n_samples=select, random_state=random_state)

    def __fit_estimator(self, hyp, X_label, y_label, L, Ly, update, **kwards):
        if update:
            _tempL = np.concatenate((X_label, L))
            _tempY = np.concatenate((y_label, Ly))
            return hyp.fit(_tempL, _tempY, **kwards)
        return hyp

    def fit(self, X, y, **kwards):
        """Build a TriTraining classifier from the training set (X, y).
        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            The training input samples.
        y : array-like of shape (n_samples,)
            The target values (class labels), -1 if unlabeled.
        Returns
        -------
        self: TriTraining
            Fitted estimator.
        """
        random_state = check_random_state(self.random_state)

        X_label, y_label, X_unlabel = get_dataset(X, y)

        hypotheses = []
        e_ = [0.5] * self._N_LEARNER
        l_ = [0] * self._N_LEARNER

        # Get a random instance for each class to keep class index
        classes = set(np.unique(y_label))
        instances = list()
        labels = list()
        for x_, y_ in zip(X_label, y_label):
            if y_ in classes:
                classes.remove(y_)
                instances.append(x_)
                labels.append(y_)
            if len(classes) == 0:
                break

        for _ in range(self._N_LEARNER):
            X_sampled, y_sampled = resample(
                X_label,
                y_label,
                replace=True,
                n_samples=self.n_samples,
                random_state=random_state,
            )

            X_sampled = np.concatenate((np.array(instances), X_sampled), axis=0)
            y_sampled = np.concatenate((np.array(labels), y_sampled), axis=0)

            hypotheses.append(
                skclone(self.base_estimator).fit(X_sampled, y_sampled, **kwards)
            )

        something_has_changed = True

        while something_has_changed:
            something_has_changed = False
            L = [[]] * self._N_LEARNER
            Ly = [[]] * self._N_LEARNER
            e = []
            updates = [False] * 3

            for i in range(self._N_LEARNER):
                hj, hk = TriTraining._another_hs(hypotheses, i)
                e.append(
                    TriTraining._measure_error(X_label, y_label, hj, hk, self._epsilon)
                )
                if e_[i] <= e[i]:
                    continue
                y_p = hj.predict(X_unlabel)
                validx = y_p == hk.predict(X_unlabel)
                L[i] = X_unlabel[validx]
                Ly[i] = y_p[validx]

                if l_[i] == 0:
                    l_[i] = math.floor(
                        safe_division(e[i], (e_[i] - e[i]), self._epsilon) + 1
                    )
                if l_[i] >= len(L[i]):
                    continue
                if e[i] * len(L[i]) < e_[i] * l_[i]:
                    updates[i] = True
                elif l_[i] > safe_division(e[i], e_[i] - e[i], self._epsilon):
                    L[i], Ly[i] = TriTraining._subsample(
                        (L[i], Ly[i]),
                        math.ceil(
                            safe_division(e_[i] * l_[i], e[i], self._epsilon) - 1
                        ),
                        random_state,
                    )
                    updates[i] = True

            hypotheses = Parallel(n_jobs=self.n_jobs)(
                delayed(self.__fit_estimator)(hypotheses[i], X_label, y_label, L[i], Ly[i], updates[i], **kwards)
                for i in range(self._N_LEARNER)
            )

            for i in range(self._N_LEARNER):
                if updates[i]:
                    e_[i] = e[i]
                    l_[i] = len(L[i])
                    something_has_changed = True

        self.h_ = hypotheses
        self.classes_ = self.h_[0].classes_
        self.columns_ = [list(range(X.shape[1]))] * self._N_LEARNER

        return self


# Done and tested
class CoTrainingByCommittee(ClassifierMixin, Ensemble, BaseEstimator):
    def __init__(
        self,
        ensemble_estimator=BaggingClassifier(),
        max_iterations=100,
        poolsize=100,
        min_instances_for_class=3,
        random_state=None,
    ):
        """Create a committee trained by cotraining based on
        the diversity of classifiers.
        M. F. A. Hady and F. Schwenker,
        "Co-training by Committee: A New Semi-supervised Learning Framework,"
        2008 IEEE International Conference on Data Mining Workshops,
        Pisa, 2008, pp. 563-572, doi: 10.1109/ICDMW.2008.27.
        Parameters
        ----------
        ensemble_estimator : ClassifierMixin, optional
            ensemble method, works without a ensemble as
            self training with pool, by default BaggingClassifier().
        max_iterations : int, optional
            number of iterations of training, -1 if no max iterations, by default 100
        poolsize : int, optional
            max number of unlabel instances candidates to pseudolabel, by default 100
        random_state : int, RandomState instance, optional
            controls the randomness of the estimator, by default None
        """
        assert isinstance(
            ensemble_estimator, ClassifierMixin
        ), "This method only support classification"
        self.ensemble_estimator = ensemble_estimator
        self.max_iterations = max_iterations
        self.poolsize = poolsize
        self.random_state = random_state
        self.min_instances_for_class = min_instances_for_class

    def fit(self, X, y, **kwards):
        """Build a CoTrainingByCommittee classifier from the training set (X, y).
        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            The training input samples.
        y : array-like of shape (n_samples,)
            The target values (class labels), -1 if unlabel.
        Returns
        -------
        self: CoTrainingByCommittee
            Fitted estimator.
        """
        self.ensemble_estimator = skclone(self.ensemble_estimator)
        random_state = check_random_state(self.random_state)

        X_label, y_prev, X_unlabel = get_dataset(X, y)

        self.label_encoder_ = LabelEncoder()
        y_label = self.label_encoder_.fit_transform(y_prev)

        self.classes_ = self.label_encoder_.classes_

        prior = calculate_prior_probability(y_label)
        permutation = random_state.permutation(len(X_unlabel))

        self.ensemble_estimator.fit(X_label, y_label, **kwards)

        for _ in range(self.max_iterations):
            if len(permutation) == 0:
                break
            raw_predictions = self.ensemble_estimator.predict_proba(
                X_unlabel[permutation[0:self.poolsize]]
            )

            predictions = np.max(raw_predictions, axis=1)
            class_predicted = np.argmax(raw_predictions, axis=1)

            added = np.zeros(predictions.shape, dtype=bool)
            # First the n (or less) most confidence instances will be selected
            for c in self.ensemble_estimator.classes_:
                condition = class_predicted == c

                candidates = predictions[condition]
                candidates_bool = np.zeros(predictions.shape, dtype=bool)
                candidates_sub_set = candidates_bool[condition]

                instances_index_selected = candidates.argsort()[
                    -self.min_instances_for_class:
                ]

                candidates_sub_set[instances_index_selected] = True
                candidates_bool[condition] += candidates_sub_set

                added[candidates_bool] = True

            # Bajo esta interpretación se garantiza que al menos existen n elemento de cada clase por iteración
            # Pero si se añaden ya en el proceso de proporción no se duplica.

            # Con esta otra interpretación ignora las n primeras instancias de cada clase
            to_label = choice_with_proportion(
                predictions, class_predicted, prior, extra=self.min_instances_for_class
            )
            added[to_label] = True

            index = permutation[0:self.poolsize][added]
            X_label = np.append(X_label, X_unlabel[index], axis=0)
            pseudoy = class_predicted[added]

            y_label = np.append(y_label, pseudoy)
            permutation = permutation[list(map(lambda x: x not in index, permutation))]

            self.ensemble_estimator.fit(X_label, y_label, **kwards)

        return self

    def predict(self, X):
        """Predict class value for X.
        For a classification model, the predicted class for each sample in X is returned.
        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            The input samples.
        Returns
        -------
        y: array-like of shape (n_samples,)
            The predicted classes
        """
        check_is_fitted(self.ensemble_estimator)
        return self.label_encoder_.inverse_transform(self.ensemble_estimator.predict(X))

    def predict_proba(self, X):
        """Predict class probabilities of the input samples X.
        The predicted class probability depends on the ensemble estimator.
        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            The input samples.
        Returns
        -------
        y: ndarray of shape (n_samples, n_classes) or list of n_outputs such arrays if n_outputs > 1
            The predicted classes
        """
        check_is_fitted(self.ensemble_estimator)
        return self.ensemble_estimator.predict_proba(X)

    def score(self, X, y, sample_weight=None):
        """Return the mean accuracy on the given test data and labels.
        In multi-label classification, this is the subset accuracy which is a harsh metric since you require for each sample that each label set be correctly predicted.
        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)
            Test samples.
        y : array-like of shape (n_samples,) or (n_samples, n_outputs)
            True labels for X.
        sample_weight : array-like of shape (n_samples,), optional
            Sample weights., by default None
        Returns
        -------
        score: float
            Mean accuracy of self.predict(X) wrt. y.
        """
        try:
            y = self.label_encoder_.transform(y)
        except ValueError:
            if "le_dict_" not in dir(self):
                self.le_dict_ = dict(
                    zip(
                        self.label_encoder_.classes_,
                        self.label_encoder_.transform(self.label_encoder_.classes_),
                    )
                )
            y = np.array(list(map(lambda x: self.le_dict_.get(x, -1), y)))

        return self.ensemble_estimator.score(X, y, sample_weight)


# Done and tested
class CoForest(_BaseCoTraining):
    def __init__(self, n_estimators=7, threshold=0.75, random_state=None, **kwards):
        """
        Li, M., & Zhou, Z.-H. (2007).
        Improve Computer-Aided Diagnosis With Machine Learning Techniques Using Undiagnosed Samples.
        <i>IEEE Transactions on Systems, Man, and Cybernetics - Part A: Systems and Humans</i>,
        37(6), 1088–1098. doi:10.1109/tsmca.2007.904745

        Parameters
        ----------
        n_estimators : int, optional
            The number of base estimators in the ensemble., by default 7
        threshold : float, optional
            The decision threshold. Should be in [0, 1)., by default 0.5
        random_state : int, RandomState instance, optional
            controls the randomness of the estimator, by default None
        """
        self._base = DecisionTreeClassifier(random_state=random_state, **kwards)
        self.n_estimators = n_estimators
        self.threshold = threshold
        self._epsilon = sys.float_info.epsilon
        self.random_state = random_state

    def __estimate_error(self, hypothesis, X, y):
        probas = hypothesis.predict_proba(X)
        ei_t = 0
        classes = list(hypothesis.classes_)
        for j in range(y.shape[0]):
            true_y = y[j]
            true_y_index = classes.index(true_y)
            ei_t += 1 - probas[j, true_y_index]
        if ei_t == 0:
            ei_t = self._epsilon
        return ei_t

    def fit(self, X, y, **kwards):
        """Build a CoForest classifier from the training set (X, y).

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            The training input samples.
        y : array-like of shape (n_samples,)
            The target values (class labels), -1 if unlabel.

        Returns
        -------
        self: CoForest
            Fitted estimator.
        """
        random_state = check_random_state(self.random_state)

        X_label, y_label, X_unlabel = get_dataset(X, y)

        hypotheses = []
        errors = []
        weights = []
        for i in range(self.n_estimators):
            hypotheses.append(skclone(self._base).fit(X_label, y_label, **kwards))
            errors.append(0.5)
            weights.append(np.max(hypotheses[i].predict_proba(X_label), axis=1).sum())

        changing = True
        while changing:
            changing = False
            for i in range(self.n_estimators):
                hi, ei, wi = hypotheses[i], errors[i], weights[i]

                ei_t = self.__estimate_error(hi, X_label, y_label)

                wi_t = wi
                if ei_t < ei:
                    random_index_subsample = list(range(X_unlabel.shape[0]))
                    random_index_subsample = random_state.permutation(
                        random_index_subsample
                    )
                    Ui_t = X_unlabel[random_index_subsample[0 : int(ei * wi / ei_t)], :]

                    raw_predictions = hi.predict_proba(Ui_t)
                    predictions = np.max(raw_predictions, axis=1)
                    class_predicted = np.array(
                        list(
                            map(
                                lambda x: hi.classes_[x],
                                np.argmax(raw_predictions, axis=1),
                            )
                        )
                    )

                    to_label = predictions > self.threshold
                    wi_t = predictions[to_label].sum()

                    if ei_t * wi_t < ei * wi:
                        changing = True
                        hi.fit(
                            np.concatenate((X_label, Ui_t[to_label])),
                            np.concatenate((y_label, class_predicted[to_label])),
                            **kwards
                        )
                errors[i] = ei_t
                weights[i] = wi_t

        self.h_ = hypotheses
        self.classes_ = self.h_[0].classes_
        self.columns_ = [list(range(X.shape[1]))] * self.n_estimators

        return self
