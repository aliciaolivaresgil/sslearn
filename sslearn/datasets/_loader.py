import pandas as pd
import warnings
from ._preprocess import secure_dataset

keel_type_cheat = {
    "string": "string",
    "integer": "int",
    "real": "float",
    "numeric": "double"
}


def read_keel(path, format="pandas", secure=True, target_col=None, encoding="utf-8", **kwards):
    """Read a .dat file from KEEL (http://www.keel.es/)

    Parameters
    ----------
    path : str
        File path
    format : str, optional
        Object that will contain the data, it can be `numpy` or `pandas`, by default "pandas"
    secure : bool, optional
        Securize de dataset for semi-supervised learning ensuring that not exists `-1` as valid class, by default True
    target_col : {str, int, None}, optional
        Column name or index to select class column, if None use the default value stored in the file, by default None
    encoding: str, optional
        Encoding of file, by default "utf-8"

    Returns
    -------
    X, y: array_like
        Dataset loaded.
    """
    if format not in ["pandas", "numpy"]:
        raise AttributeError("Formats allowed are `pandas` or `numpy`")

    attributes = []
    types = []
    target = None
    with open(path, "r") as file:
        lines = file.readlines()
        counter = 1
        for line in lines:
            counter += 1
            if "@attribute" in line:
                parts = line.split(" ")
                name_ = parts[1]
                type_ = parts[2]
                if type_[0] == "{":
                    type_ = "string"
                attributes.append(name_)
                types.append(keel_type_cheat[type_])
            elif "@outputs" in line:
                target = line.split(" ")[1].strip('\n')
            elif "@data" in line:
                break
    if target is None:
        target = attributes[-1]
    data = pd.read_csv(path, skiprows=counter, header=None, **kwards)
    if len(data.columns) != len(attributes):
        warnings.warn(f"The dataset's have {len(data.columns)} columns but file declares {len(attributes)}.", RuntimeWarning)
        X = data
        y = None
    else:
        data.columns = attributes
        data = data.astype(dict(zip(attributes, types)))
        for att, tp in zip(attributes, types):
            if tp == "string":
                data[att] = data[att].str.strip()
        if target_col is None:
            target_col = target
        elif isinstance(target_col, int):
            target_col = data.columns[target_col]

        att_columns = attributes.copy()
        att_columns.remove(target_col)

        X = data[att_columns]
        y = data[target_col]

        y[y == "unlabeled"] = y.dtype.type(-1)
        if secure:
            X, y = secure_dataset(X, y)

    if format == "numpy":
        X = X.to_numpy()
        y = y.to_numpy()
        if y.dtype == object:
            y = y.astype("str")
    return X, y


def read_csv(path, format="pandas", secure=True, target_col=-1, **kwards):
    """Read a .csv file

    Parameters
    ----------
    path : str
        File path
    format : str, optional
        Object that will contain the data, it can be `numpy` or `pandas`, by default "pandas"
    secure : bool, optional
        Securize de dataset for semi-supervised learning ensuring that not exists `-1` as valid class, by default True
    target_col : {str, int, None}, optional
        Column name or index to select class column, if None use the default value stored in the file, by default None

    Returns
    -------
    X, y: array_like
        Dataset loaded.
    """
    if format not in ["pandas", "numpy"]:
        raise AttributeError("Formats allowed are `pandas` or `numpy`")
    data = pd.read_csv(path, **kwards)

    if target_col is None:
        raise AttributeError("`read_csv` do not allow a `None` value for `target_col`, use `integer` or `string` instead.")
    elif isinstance(target_col, str):
        target_col = data.columns.index(target_col)

    X = data.loc[:, data.columns != data.columns[target_col]]
    y = data.loc[:, target_col]

    if secure:
        X, y = secure_dataset(X, y, target_column=target_col)
    if format == "numpy":
        X = X.to_numpy()
        y = y.to_numpy()
    return X, y


def read_arff(path, format="pandas", secure=True, target_col=-1):
    """Read .arff file from WEKA. It requires `arff2pandas`
    Parameters
    ----------
    path : string
        File path
    format : str, optional
        The kind of data structure to load the file, may be `pandas` for DataFrame or `numpy` for array , by default "pandas"
    secure : bool, optional
        If `secure` is True then if exists a -1 value in target classes the target values will be increased in two values., by default True
    target_col : int, optional
        Select the column to mark as target. If is -1 then the last column will be selected. , by default -1

    Returns
    -------
    X, y: array_like
        Dataset loaded.
    """
    from arff2pandas import a2p

    if format not in ["pandas", "numpy"]:
        raise AttributeError("Formats allowed are `pandas` or `numpy`")

    with open(path, "r") as file:
        data = a2p.load(file)

    X = data.loc[:, data.columns != data.columns[target_col]]
    y = data.loc[:, target_col]

    if secure:
        X, y = secure_dataset(X, y, target_column=target_col)
    if format == "numpy":
        X = X.to_numpy()
        y = y.to_numpy()
    return X, y