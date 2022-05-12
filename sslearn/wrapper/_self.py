from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.neighbors import KNeighborsClassifier
from sklearn.semi_supervised import SelfTrainingClassifier
from sklearn.base import clone as skclone
from sklearn.utils import check_random_state, resample
import numpy as np
from ..base import get_dataset
from sklearn.neighbors import kneighbors_graph
from sslearn.utils import calculate_prior_probability
from scipy.stats import norm


SelfTraining = SelfTrainingClassifier


class Setred(ClassifierMixin, BaseEstimator):
    def __init__(
        self,
        base_estimator=KNeighborsClassifier(n_neighbors=3),
        max_iterations=40,
        distance="euclidean",
        poolsize=0.25,
        rejection_threshold=0.05,
        graph_neighbors=1,
        random_state=None,
        n_jobs=None,
    ):
        """
        Li, Ming, and Zhi-Hua Zhou. "SETRED: Self-training with editing."
        Pacific-Asia Conference on Knowledge Discovery and Data Mining.
        Springer, Berlin, Heidelberg, 2005. doi: 10.1007/11430919_71.

        Parameters
        ----------
        base_estimator : ClassifierMixin, optional
            An estimator object implementing fit and predict_proba,, by default DecisionTreeClassifier(), by default KNeighborsClassifier(n_neighbors=3)
        max_iterations : int, optional
            Maximum number of iterations allowed. Should be greater than or equal to 0., by default 40
        distance : str, optional
            The distance metric to use for the graph.
            The default metric is euclidean, and with p=2 is equivalent to the standard Euclidean metric.
            For a list of available metrics, see the documentation of DistanceMetric and the metrics listed in sklearn.metrics.pairwise.PAIRWISE_DISTANCE_FUNCTIONS.
            Note that the “cosine” metric uses cosine_distances., by default "euclidean"
        poolsize : float, optional
            Max number of unlabel instances candidates to pseudolabel, by default 0.25
        rejection_threshold : float, optional
            significance level, by default 0.1
        graph_neighbors : int, optional
            Number of neighbors for each sample., by default 1
        random_state : int, RandomState instance, optional
            controls the randomness of the estimator, by default None
        n_jobs : int, optional
            The number of parallel jobs to run for neighbors search. None means 1 unless in a joblib.parallel_backend context. -1 means using all processors, by default None
        """
        self.base_estimator = base_estimator
        self.max_iterations = max_iterations
        self.poolsize = poolsize
        self.distance = distance
        self.rejection_threshold = rejection_threshold
        self.graph_neighbors = graph_neighbors
        self.random_state = random_state
        self.n_jobs = n_jobs

    def __create_neighborhood(self, X):
        # kneighbors_graph(X, 1, metric=self.distance, n_jobs=self.n_jobs).toarray()
        return kneighbors_graph(
            X, self.graph_neighbors, metric=self.distance, n_jobs=self.n_jobs, mode="distance"
        ).toarray()

    def fit(self, X, y, **kwars):
        """Build a Setred classifier from the training set (X, y).

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            The training input samples.
        y : array-like of shape (n_samples,)
            The target values (class labels), -1 if unlabeled.

        Returns
        -------
        self: Setred
            Fitted estimator.
        """        
        random_state = check_random_state(self.random_state)

        X_label, y_label, X_unlabel = get_dataset(X, y)
        each_iteration_candidates = X_label.shape[0]

        pool = int(len(X_unlabel) * self.poolsize)
        self._base_estimator = skclone(self.base_estimator)

        self._base_estimator.fit(X_label, y_label, **kwars)

        y_probabilities = calculate_prior_probability(
            y_label
        )  # Should probabilities change every iteration or may it keep with the first L?

        sort_idx = np.argsort(list(y_probabilities.keys()))

        for _ in range(self.max_iterations):
            U_ = resample(
                X_unlabel, replace=False, n_samples=pool, random_state=random_state
            )

            raw_predictions = self._base_estimator.predict_proba(U_)
            predictions = np.max(raw_predictions, axis=1)
            class_predicted = np.argmax(raw_predictions, axis=1)
            # Unless a better understanding is given, only the size of L will be used as maximal size of the candidate set.
            indexes = predictions.argsort()[-each_iteration_candidates:]

            L_ = U_[indexes]
            y_ = np.array(
                list(
                    map(
                        lambda x: self._base_estimator.classes_[x],
                        class_predicted[indexes],
                    )
                )
            )

            pre_L = np.concatenate((X_label, L_), axis=0)

            weights = self.__create_neighborhood(pre_L)
            #  Keep only weights for L_
            weights = weights[-L_.shape[0]:, :]

            idx = np.searchsorted(np.array(list(y_probabilities.keys())), y_, sorter=sort_idx)
            p_wrong = 1 - np.asarray(np.array(list(y_probabilities.values())))[sort_idx][idx]
            #  Must weights be the inverse of distance?
            weights = np.divide(1, weights, out=np.zeros_like(weights), where=weights != 0)

            weights_sum = weights.sum(axis=1)
            weights_square_sum = (weights ** 2).sum(axis=1)

            iid_random = random_state.binomial(
                1, np.repeat(p_wrong, weights.shape[1]).reshape(weights.shape)
            )
            ji = (iid_random * weights).sum(axis=1)

            mu_h0 = p_wrong * weights_sum
            sigma_h0 = np.sqrt((1 - p_wrong) * p_wrong * weights_square_sum)
            
            z_score = np.divide((ji - mu_h0), sigma_h0, out=np.zeros_like(sigma_h0), where=sigma_h0 != 0)
            # z_score = (ji - mu_h0) / sigma_h0

            oi = norm.sf(abs(z_score), mu_h0, sigma_h0)
            to_add = (oi < self.rejection_threshold) & (z_score < mu_h0)

            L_filtered = L_[to_add, :]
            y_filtered = y_[to_add]

            X_label = np.concatenate((X_label, L_filtered), axis=0)
            y_label = np.concatenate((y_label, y_filtered), axis=0)

            #  Remove the instances from the unlabeled set.
            to_delete = indexes[to_add]
            X_unlabel = np.delete(X_unlabel, to_delete, axis=0)

        return self

    def predict(self, X, **kwards):
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
        return self._base_estimator.predict(X, **kwards)

    def predict_proba(self, X, **kwards):
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
        return self._base_estimator.predict_proba(X, **kwards)
