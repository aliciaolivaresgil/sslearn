from scipy.sparse.construct import random
from scipy.sparse.sputils import isintlike
from sklearn.base import ClassifierMixin, RegressorMixin, BaseEstimator
from sklearn.tree import DecisionTreeClassifier
from sklearn.ensemble import BaggingClassifier
import numpy as np
from sklearn.utils.validation import check_is_fitted
from sklearn.utils import check_random_state, resample
from sklearn.exceptions import NotFittedError, ConvergenceWarning
from sklearn.base import clone as skclone
import math
from abc import ABC, abstractmethod
from sklearn.base import MetaEstimatorMixin
from sklearn.multiclass import LabelBinarizer
from sklearn.feature_selection import mutual_info_classif
from ..utils import calculate_prior_probability
import sys
import operator
from sklearn.discriminant_analysis import softmax
from sslearn.supervised import rotation as rot
from sklearn.decomposition import PCA
from statsmodels.stats.proportion import proportion_confint
import warnings
from sslearn.base import Ensemble
from sslearn.utils import check_n_jobs
from joblib import Parallel, delayed


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
            for i in range(len(self.h_)):
                ys.append(self.h_[i].predict_proba(
                    X[:, self.columns_[i]]), **kwards)
            y = (sum(ys)/len(ys))
            return y
        else:
            raise NotFittedError("Classifier not fitted")


# Done and tested
class DemocraticCoLearning(_BaseCoTraining):

    def __init__(self, base_estimator=DecisionTreeClassifier(),
                 n_estimators=3):

        if isinstance(base_estimator, ClassifierMixin) and n_estimators is not None:
            estimators = list()
            for _ in range(n_estimators):
                estimators.append(skclone(base_estimator))
            self.base_estimator = estimators
        elif isinstance(base_estimator, list):
            self.base_estimator = base_estimator
        else:
            raise AttributeError(
                "If `n_estimators` is None then `base_estimator` must be a `list`.")
        self.n_estimators = len(self.base_estimator)

    def __vote_ponderate(self, x, confidence, H):
        predicted = []
        ponderated = {}
        for i, h in enumerate(H):
            w = confidence[i][-1]
            y = h.predict(x.reshape(1, -1))
            if y[0] not in ponderated:
                ponderated[y[0]] = w
            else:
                ponderated[y[0]] += w
            predicted.append(y)
        return max(ponderated.items(), key=operator.itemgetter(1))[0], predicted

    def __calcule_last_confidences(self, X, y):
        w = []
        for H in self.h_:
            successes = len(H.predict(X) == y)
            trials = len(X)
            li, hi = proportion_confint(successes, trials)
            w.append((li + hi)/2)
        self.confidences_ = w

    def fit(self, X, y, estimator_kwards=None):
        if estimator_kwards is None:
            estimator_kwards = [{} for _ in range(self.n_estimators)]
        X_label = X[y != y.dtype.type(-1)]
        y_label = y[y != y.dtype.type(-1)]
        X_unlabel = X[y == y.dtype.type(-1)]

        L = [X_label] * self.n_estimators
        Ly = [y_label] * self.n_estimators
        e = [0] * self.n_estimators

        changed = True
        while changed:
            changed = False
            for i in range(self.n_estimators):
                self.base_estimator[i].fit(L[i], Ly[i], **estimator_kwards[i])

            ##################################
            # Estos pasos parecen sobrar #####
            temp = list()
            for i in range(self.n_estimators):
                temp.extend(list(self.base_estimator[i].predict(X_unlabel)))
            k = np.unique(np.array(temp), return_counts=True)
            ##################################

            L_ = list()
            Ly_ = list()
            confidence = [()] * self.n_estimators
            for i, H in enumerate(self.base_estimator):
                successes = len(H.predict(X_label) == y_label)
                trials = len(X_label)
                li, hi = proportion_confint(successes, trials)
                wi = (li + hi)/2
                confidence[i] = li, hi, wi

                L_.append([])
                Ly_.append([])

            def check_if_add_to_L(x):
                y, predicted = self.__vote_ponderate(
                    x, confidence, self.base_estimator)
                return y == predicted, y

            instance_to_add, label_to_add = np.apply_along_axis(
                check_if_add_to_L, 1, X_unlabel)
            for i in range(self.n_estimators):
                L_[i] = np.concatenate(
                    (L_[i], X_unlabel[:, instance_to_add[i]]), axis=0)
                Ly_[i] = np.concatenate(
                    (L_[i], label_to_add[instance_to_add[i]]), axis=0)

            new_confidences = []
            e_factor = 0
            for i, H in enumerate(self.base_estimator):
                successes = len(H.predict(L[i]) == Ly[i])
                trials = len(L[i])
                new_confidences.append(proportion_confint(successes, trials))
                e_factor += new_confidences[-1][0]
            e_factor = 1 - e_factor / len(new_confidences)

            for i, _ in enumerate(self.base_estimator):
                if len(L_[i]) > 0:
                    li, hi = new_confidences[i]

                    qi = len(L[i])*((1 - 2*(e[i]/len(L[i])))**2)
                    e_i = e_factor * len(L_[i])

                    q_i = (len(L[i])+len(L_[i])) * \
                        (1 - (2*e[i]+e_i)/(len(L[i])+len(L_[i])))

                    if q_i > qi:
                        L[i] = np.concatenate((L[i], np.array(L_[i])))
                        Ly[i] = np.concatenate((Ly[i], np.array(Ly_[i])))
                        e[i] = e[i]+e_i
                        changed = True

        self.h_ = self.base_estimator
        self.classes_ = self.h_[0].classes_
        self.__calcule_last_confidences(X_label, y_label)
        self.columns_ = [list(range(X.shape[1]))]*self.n_estimators

        return self

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
            y_ = list()
            if len(X) == 1:
                X = [X]
            for x in X:
                groups = dict(
                    zip(self.classes_, [list() for _ in range(len(self.classes_))]))
                for w, H in zip(self.confidences_, self.h_):
                    cj = H.predict(x.reshape(1, -1))
                    if w > 0.5:
                        groups[cj[0]].append(w)
                C_G_j = list()
                for c in self.classes_:
                    size = len(groups[c])
                    if size == 0:
                        cgj = 0.5
                    else:
                        cgj = ((size+0.5)/(size+1)) * (sum(groups[c])/size)

                    C_G_j.append(cgj)
                y_.append(softmax(np.array(C_G_j).reshape(1, -1))[0])
            return np.array(y_)
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

    def __init__(self, base_estimator=DecisionTreeClassifier(),
                 second_base_estimator=None, max_iterations=30,
                 poolsize=75, positives=-1, negatives=-1,
                 random_state=None):
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
            base_estimator, ClassifierMixin), "This method only support classification"

        self.base_estimator = base_estimator
        self.second_base_estimator = second_base_estimator

        self.max_iterations = max_iterations
        self.poolsize = poolsize
        self.random_state = random_state

        if (positives == -1 and negatives != -1) or \
           (positives != -1 and negatives == -1):
            raise ValueError(
                'Current implementation supports either both positives and negatives being specified, or neither')

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
        assert not (X2 is not None and features is not None),\
            "The list of features and x2 cannot be defined at the same time"
        X1 = X
        if X2 is None and features is None:
            X2 = X.copy()
            self.columns_ = [list(range(X.shape[1]))]*2
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
                self.negatives = round(self.positives*n_p_ratio)

            else:
                self.negatives = 1
                self.positives = round(self.negatives/n_p_ratio)

        assert(self.positives > 0 and self.negatives > 0 and
               self.max_iterations > 0 and self.poolsize > 0), \
            "Parameters are inconsistent"

        # Set of unlabeled samples
        U = [i for i, y_i in enumerate(y) if y_i == -1]
        rs.shuffle(U)

        U_ = U[-min(len(U), self.poolsize):]
        # remove the samples in U_ from U
        U = U[:-len(U_)]

        L = [i for i, y_i in enumerate(y) if y_i != -1]

        y = y.reshape((y.shape[0],1))

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

            for i in (y1_prob[:, 0].argsort())[-self.negatives:]:
                if y1_prob[i, 0] > 0.5:
                    n.append(i)
            for i in (y1_prob[:, 1].argsort())[-self.positives:]:
                if y1_prob[i, 1] > 0.5:
                    p.append(i)

            for i in (y2_prob[:, 0].argsort())[-self.negatives:]:
                if y2_prob[i, 0] > 0.5:
                    n.append(i)
            for i in (y2_prob[:, 1].argsort())[-self.positives:]:
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
            y = (sum(ys)/len(ys))
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
                (np.argmax(predicted_probabilitiy, axis=1)), axis=0)
        return self.label_binarize.inverse_transform(result)


# Done and tested
class Rasco(_BaseCoTraining):

    def __init__(self, base_estimator=DecisionTreeClassifier(),
                 max_iterations=10, n_estimators=30, incremental=True,
                 batch_size=None, subspace_size=None, random_state=None, n_jobs=None):
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
        assert isinstance(base_estimator, ClassifierMixin),\
            "This method only support classification"
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
            idxs.append(random_state.permutation(
                features)[:self.subspace_size])
        return idxs

    def __fit_estimator(self, X, y, **kwards):
        return skclone(self.base_estimator).fit(X, y,  **kwards)

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
        X_label = X[y != y.dtype.type(-1)]
        y_label = y[y != y.dtype.type(-1)]
        X_unlabel = X[y == y.dtype.type(-1)]

        random_state = check_random_state(self.random_state)

        if not self.incremental and self.batch_size is None:
            self.batch_size = X_label.shape[0]

        if self.subspace_size is None:
            self.subspace_size = int(X.shape[1]/2)
        idxs = self._generate_random_subspaces(X_label, y_label, random_state)

        cfs = Parallel(n_jobs=self.n_jobs)(
            delayed(self.__fit_estimator)(
                X_label[:, idxs[i]], y_label, **kwards
            )
            for i in range(self.n_estimators)
        )

        it = 0
        while True:
            if (self.max_iterations != -1 and it >= self.max_iterations) or len(X_unlabel) == 0:
                break

            raw_predicions = []
            for i in range(self.n_estimators):
                rp = cfs[i].predict_proba(X_unlabel[:, idxs[i]])
                raw_predicions.append(rp)
            raw_predicions = sum(raw_predicions) / self.n_estimators
            predictions = np.max(raw_predicions, axis=1)
            class_predicted = np.argmax(raw_predicions, axis=1)
            pseudoy = np.array(
                list(map(lambda x: cfs[0].classes_[x], class_predicted)))

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
                        warnings.warn("RASCO convergence warning, the class " +
                                      str(class_) + " not predicted", ConvergenceWarning)
                Lj = np.array(Lj)
            else:
                Lj = sorted_[- self.batch_size:]
                yj = pseudoy[Lj]

            X_label = np.append(X_label, X_unlabel[Lj, :], axis=0)
            y_label = np.append(y_label, yj)
            X_unlabel = np.delete(X_unlabel, Lj, axis=0)

            cfs = Parallel(n_jobs=self.n_jobs)(
                delayed(self.__fit_estimator)(
                    X_label[:, idxs[i]], y_label, **kwards
                )
                for i in range(self.n_estimators)
            )

            it += 1

        self.h_ = cfs
        self.classes_ = self.h_[0].classes_
        self.columns_ = idxs

        return self


# Done and tested
class RelRasco(Rasco):

    def __init__(self, base_estimator=DecisionTreeClassifier(),
                 max_iterations=10, n_estimators=30, incremental=True,
                 batch_size=None, subspace_size=None, random_state=None, n_jobs=None):
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
        """
        super().__init__(base_estimator, max_iterations, n_estimators,
                         incremental, batch_size, subspace_size, random_state, n_jobs)

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


class RotRelRasco(RelRasco):
    def __init__(self, base_estimator=DecisionTreeClassifier(),
                 group_weight=0.5, pca=PCA(), pre_rotation=False,
                 max_iterations=10, n_estimators=30, incremental=True,
                 batch_size=None, subspace_size=None, random_state=None):
        """
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
        super().__init__(base_estimator, max_iterations, n_estimators,
                         incremental, batch_size, subspace_size, random_state)
        self.group_weight = group_weight
        self.pca = pca
        self.pre_rotation = pre_rotation

    def fit(self, X, y, **kwards):
        """Build a RotRelRasco classifier from the training set (X, y).

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
        self._rotations = dict()

        random_state = check_random_state(self.random_state)

        X_label = X[y != y.dtype.type(-1)]
        y_label = y[y != y.dtype.type(-1)]
        X_unlabel = X[y == y.dtype.type(-1)]

        if not self.incremental and self.batch_size is None:
            self.batch_size = X_label.shape[0]

        if self.subspace_size is None:
            self.subspace_size = int(X.shape[1]/2)

        if self.pre_rotation:
            self._rotations = rot.Rotation(math.ceil(
                X.shape[1]/self.subspace_size), self.group_weight, skclone(self.pca), random_state=random_state)
            X_unlabel = self._rotations.fit_transform(X_unlabel)
            X_label = self._rotations.transform(X_label)

        idxs = self._generate_random_subspaces(
            X_label, y_label, random_state=random_state)

        if not self.pre_rotation:
            for idx in idxs:
                self.__rotate(X_unlabel, idx, fit=True,
                              random_state=random_state)

        cfs = []

        for i in range(self.n_estimators):
            if self.pre_rotation:
                r = X_label[:, idxs[i]]
            else:
                r = self.__rotate(X_label, idxs[i])

            cfs.append(skclone(self.base_estimator)
                       .fit(r, y_label, **kwards))

        it = 0
        while True:
            if (self.max_iterations != -1 and it >= self.max_iterations) or len(X_unlabel) == 0:
                break

            raw_predicions = []
            for i in range(self.n_estimators):
                if self.pre_rotation:
                    r = X_unlabel[:, idxs[i]]
                else:
                    r = self.__rotate(X_unlabel, idxs[i])
                rp = cfs[i].predict_proba(r)
                raw_predicions.append(rp)
            raw_predicions = sum(raw_predicions)/self.n_estimators
            predictions = np.max(raw_predicions, axis=1)
            class_predicted = np.argmax(raw_predicions, axis=1)
            pseudoy = np.array(
                list(map(lambda x: cfs[0].classes_[x], class_predicted)))

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
                        warnings.warn("RASCO convergence warning, the class " + 
                                      str(class_) + " not predicted", ConvergenceWarning)

                Lj = np.array(Lj)
            else:
                Lj = sorted_[- self.batch_size:]
                yj = pseudoy[Lj]

            X_label = np.append(X_label, X_unlabel[Lj, :], axis=0)
            y_label = np.append(y_label, yj)
            X_unlabel = np.delete(X_unlabel, Lj, axis=0)

            for i in range(self.n_estimators):
                if self.pre_rotation:
                    r = X_label[:, idxs[i]]
                else:
                    r = self.__rotate(X_label, idxs[i])
                cfs[i].fit(r, y_label, **kwards)

            it += 1

        self.h_ = cfs
        self.classes_ = self.h_[0].classes_
        self.columns_ = idxs

        return self

    def __rotate(self, X, idx, fit=False, random_state=None):
        if fit:
            rt = rot.Rotation(len(idx), self.group_weight, skclone(
                self.pca), random_state=random_state)
            self._rotations[tuple(idx)] = rt
            rt.fit(X[:, idx])
        else:
            rt = self._rotations[tuple(idx)]
        X_rotated = rt.transform(X[:, idx])
        return X_rotated

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
            if self.pre_rotation:
                X = self._rotations.transform(X)
            for i in range(len(self.h_)):
                if self.pre_rotation:
                    r = X[:, self.columns_[i]]
                else:
                    r = self.__rotate(X, self.columns_[i])
                ys.append(self.h_[i].predict_proba(
                    r, **kwards))
            y = (sum(ys)/len(ys))
            return y
        else:
            raise NotFittedError("Classifier not fitted")


# Done and tested
class TriTraining(_BaseCoTraining):

    def __init__(self, base_estimator=DecisionTreeClassifier(),
                 n_samples=None, random_state=None):
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
        self.random_state = random_state

    @staticmethod
    def _measure_error(X, y, h1: ClassifierMixin, h2: ClassifierMixin):
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

        y1_fail = np.logical_not(y1 == y)
        y2_fail = np.logical_not(y2 == y)

        error = np.count_nonzero(np.logical_and(y1_fail, y2_fail))
        coincidence = np.count_nonzero(y1 == y2)

        return error / coincidence

    @staticmethod
    def _another_hs(hs, index):
        """Get the another hypothesis

        Parameters
        ----------
        hs : list
            hypothesis collection
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
        return resample(*L, replace=False,
                        n_samples=len(L)-s, random_state=random_state)

    def fit(self, X, y, **kwards):
        """Build a TriTraining classifier from the training set (X, y).

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            The training input samples.
        y : array-like of shape (n_samples,)
            The target values (class labels), -1 if unlabel.

        Returns
        -------
        self: TriTraining
            Fitted estimator.
        """
        random_state = check_random_state(self.random_state)

        X_label = X[y != y.dtype.type(-1)]
        y_label = y[y != y.dtype.type(-1)]
        X_unlabel = X[y == y.dtype.type(-1)]

        hypothesis = []
        e = [.5] * self._N_LEARNER
        l_ = [0] * self._N_LEARNER

        for _ in range(self._N_LEARNER):
            X_sampled, y_sampled = \
                resample(X_label, y_label, replace=True,
                         n_samples=self.n_samples,
                         random_state=random_state)

            hypothesis.append(
                skclone(self.base_estimator).fit(
                    X_sampled, y_sampled, **kwards)
            )

        something_has_changed = True

        while something_has_changed:
            something_has_changed = False
            L = [[]] * self._N_LEARNER
            Ly = [[]] * self._N_LEARNER
            _e = []
            updates = [False] * 3

            for i in range(self._N_LEARNER):
                hj, hk = TriTraining._another_hs(hypothesis, i)
                _e.append(TriTraining._measure_error(X_label, y_label, hj, hk))
                if e[i] > _e[i]:
                    y_p = hj.predict(X_unlabel)
                    validx = y_p == hk.predict(X_unlabel)
                    L[i] = X_unlabel[validx]
                    Ly[i] = y_p[validx]

                    if l_[i] == 0:
                        l_[i] = math.floor(_e[i] / (e[i] - _e[i]) + 1)
                    if l_[i] < len(L[i]):
                        if _e[i] * len(L[i]) < e[i] * l_[i]:
                            updates[i] = True
                        elif l_[i] > (_e[i] / (e[i] - _e[i])):
                            L[i], Ly[i] = \
                                TriTraining\
                                ._subsample((L[i], Ly[i]),
                                            math.ceil(e[i] * l_[i] / _e[i] - 1),
                                            random_state)
                            updates[i] = True

            for i in range(self._N_LEARNER):
                if updates[i]:
                    _tempL = np.concatenate((X_label, L[i]))
                    _tempY = np.concatenate((y_label, Ly[i]))
                    hypothesis[i].fit(_tempL, _tempY, **kwards)
                    e[i] = _e[i]
                    l_[i] = len(L[i])
                    something_has_changed = True

        self.h_ = hypothesis
        self.classes_ = self.h_[0].classes_
        self.columns_ = [list(range(X.shape[1]))]*self._N_LEARNER

        return self


# Done and tested
class CoTrainingByCommittee(ClassifierMixin, Ensemble, BaseEstimator):

    def __init__(self, ensemble_estimator=BaggingClassifier(),
                 max_iterations=100, poolsize=100, random_state=None):
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
            ensemble_estimator, ClassifierMixin), "This method only support classification"
        self.ensemble_estimator = skclone(ensemble_estimator)
        self.max_iterations = max_iterations
        self.poolsize = poolsize
        self.random_state = random_state

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
        random_state = check_random_state(self.random_state)
        X_label = X[y != y.dtype.type(-1)]
        y_label = y[y != y.dtype.type(-1)]
        X_unlabel = X[y == y.dtype.type(-1)]
        prior = calculate_prior_probability(y_label)
        permutation = random_state.permutation(len(X_unlabel))

        self.ensemble_estimator.fit(X_label, y_label, **kwards)
        self.classes_ = self.ensemble_estimator.classes_

        for _ in range(self.max_iterations):
            if len(permutation) == 0:
                break
            raw_predictions = \
                self.ensemble_estimator.predict_proba(
                    X_unlabel[permutation[0:self.poolsize]])

            predictions = np.max(raw_predictions, axis=1)
            class_predicted = np.argmax(raw_predictions, axis=1)

            to_label = None
            for c in range(len(self.classes_)):
                to_add = np.logical_and(
                    class_predicted == c,
                    predictions >= prior[self.classes_[c]]
                )
                if to_label is not None:
                    to_label = np.logical_or(to_label, to_add)
                else:
                    to_label = to_add

            index = permutation[0:self.poolsize][to_label]
            X_label = np.append(X_label, X_unlabel[index], axis=0)
            pseudoy = np.array(list(map(lambda x: self.classes_[x],
                                        class_predicted[to_label])))
            y_label = np.append(y_label, pseudoy, axis=0)
            permutation = permutation[
                list(map(lambda x: x not in index, permutation))
            ]

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
        return self.ensemble_estimator.predict(X)

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
        self._base = DecisionTreeClassifier(
            random_state=random_state, **kwards)
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
            ei_t += 1-probas[j, true_y_index]
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

        X_label = X[y != y.dtype.type(-1)]
        y_label = y[y != y.dtype.type(-1)]
        X_unlabel = X[y == y.dtype.type(-1)]

        hypothesis = []
        errors = []
        weights = []
        for i in range(self.n_estimators):
            hypothesis.append(skclone(self._base).fit(
                X_label, y_label, **kwards))
            errors.append(.5)
            weights.append(
                np.max(hypothesis[i].predict_proba(X_label), axis=1).sum())

        changing = True
        while changing:
            changing = False
            for i in range(self.n_estimators):
                hi, ei, wi = hypothesis[i], errors[i], weights[i]

                ei_t = self.__estimate_error(hi, X_label, y_label)

                wi_t = wi
                if ei_t < ei:
                    random_index_subsample = list(range(X_unlabel.shape[0]))
                    random_index_subsample = random_state.permutation(
                        random_index_subsample)
                    Ui_t = X_unlabel[random_index_subsample[0:int(
                        ei*wi/ei_t)], :]

                    raw_predictions = hi.predict_proba(Ui_t)
                    predictions = np.max(raw_predictions, axis=1)
                    class_predicted = np.array(
                        list(map(lambda x: hi.classes_[x], np.argmax(raw_predictions, axis=1))))

                    to_label = predictions > self.threshold
                    wi_t = predictions[to_label].sum()

                    if ei_t * wi_t < ei * wi:
                        changing = True
                        hi.fit(np.concatenate((X_label, Ui_t[to_label])), np.concatenate(
                            (y_label, class_predicted[to_label])), **kwards)
                errors[i] = ei_t
                weights[i] = wi_t

        self.h_ = hypothesis
        self.classes_ = self.h_[0].classes_
        self.columns_ = [list(range(X.shape[1]))]*self.n_estimators

        return self
