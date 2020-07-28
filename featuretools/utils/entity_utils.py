import warnings
from datetime import datetime

import dask.dataframe as dd
import numpy as np
import pandas as pd
import pandas.api.types as pdtypes

from featuretools.variable_types import (
    Boolean,
    Categorical,
    Datetime,
    Discrete,
    LatLong,
    Numeric,
    PandasTypes,
    Text,
    Unknown
)
from featuretools.variable_types.utils import convert_vtypes


def infer_variable_types(df, link_vars, variable_types, time_index, secondary_time_index):
    '''Infer variable types from dataframe

    Args:
        df (DataFrame): Input DataFrame
        link_vars (list[]): Linked variables
        variable_types (dict[str -> dict[str -> type]]) : An entity's
            variable_types dict maps string variable ids to types (:class:`.Variable`)
            or (type, kwargs) to pass keyword arguments to the Variable.
        time_index (str or None): Name of time_index column
        secondary_time_index (dict[str: [str]]): Dictionary of secondary time columns
            that each map to a list of columns that depend on that secondary time
    '''
    # TODO: set pk and pk types here
    inferred_types = {}
    vids_to_assume_datetime = [time_index]
    if len(list(secondary_time_index.keys())):
        vids_to_assume_datetime.append(list(secondary_time_index.keys())[0])
    inferred_type = Unknown
    for variable in df.columns:
        if variable in variable_types:
            continue
        elif isinstance(df, dd.DataFrame):
            msg = 'Variable types cannot be inferred from Dask DataFrames, ' \
                  'use variable_types to provide type metadata for entity'
            raise ValueError(msg)
        elif variable in vids_to_assume_datetime:
            if col_is_datetime(df[variable]):
                inferred_type = Datetime
            else:
                inferred_type = Numeric

        elif variable in link_vars:
            inferred_type = Categorical

        elif df[variable].dtype == "object":
            if not len(df[variable]):
                inferred_type = Categorical
            elif col_is_datetime(df[variable]):
                inferred_type = Datetime
            else:
                inferred_type = Categorical

                # heuristics to predict this some other than categorical
                sample = df[variable].sample(min(10000, len(df[variable])))

                # catch cases where object dtype cannot be interpreted as a string
                try:
                    avg_length = sample.str.len().mean()
                    if avg_length > 50:
                        inferred_type = Text
                except AttributeError:
                    pass

        elif df[variable].dtype == "bool":
            inferred_type = Boolean

        elif pdtypes.is_categorical_dtype(df[variable].dtype):
            inferred_type = Categorical

        elif pdtypes.is_numeric_dtype(df[variable].dtype):
            inferred_type = Numeric

        elif col_is_datetime(df[variable]):
            inferred_type = Datetime

        elif len(df[variable]):
            sample = df[variable] \
                .sample(min(10000, df[variable].nunique(dropna=False)))

            unique = sample.unique()
            percent_unique = sample.size / len(unique)

            if percent_unique < .05:
                inferred_type = Categorical
            else:
                inferred_type = Numeric

        inferred_types[variable] = inferred_type

    return inferred_types


def convert_all_variable_data(df, variable_types):
    """Convert all dataframes' variables to different types.
    """
    for var_id, desired_type in variable_types.items():
        type_args = {}
        if isinstance(desired_type, tuple):
            # grab args before assigning type
            type_args = desired_type[1]
            desired_type = desired_type[0]

        if var_id not in df.columns:
            raise LookupError("Variable ID %s not in DataFrame" % (var_id))
        current_type = df[var_id].dtype.name

        if issubclass(desired_type, Numeric) and \
                current_type not in PandasTypes._pandas_numerics:
            df = convert_variable_data(df=df,
                                       column_id=var_id,
                                       new_type=desired_type,
                                       **type_args)

        if issubclass(desired_type, Discrete) and \
                current_type not in [PandasTypes._categorical]:
            df = convert_variable_data(df=df,
                                       column_id=var_id,
                                       new_type=desired_type,
                                       **type_args)

        if issubclass(desired_type, Datetime) and \
                current_type not in PandasTypes._pandas_datetimes:
            df = convert_variable_data(df=df,
                                       column_id=var_id,
                                       new_type=desired_type,
                                       **type_args)

        # Fill in any single `NaN` values in LatLong variables with a tuple
        if issubclass(desired_type, LatLong) and isinstance(df[var_id], pd.Series) and df[var_id].hasnans:
            df[var_id] = replace_latlong_nan(df[var_id])
            warnings.warn("LatLong columns should contain only tuples. All single 'NaN' values in column '{}' have been replaced with '(NaN, NaN)'.".format(var_id))

    return df


def convert_variable_data(df, column_id, new_type, **kwargs):
    """Convert dataframe's variable to different type.
    """
    empty = df[column_id].empty if isinstance(df, pd.DataFrame) else False
    if empty:
        return df
    if new_type == Numeric:
        if isinstance(df, dd.DataFrame):
            df[column_id] = dd.to_numeric(df[column_id], errors='coerce')
        else:
            orig_nonnull = df[column_id].dropna().shape[0]
            df[column_id] = pd.to_numeric(df[column_id], errors='coerce')
            # This will convert strings to nans
            # If column contained all strings, then we should
            # just raise an error, because that shouldn't have
            # been converted to numeric
            nonnull = df[column_id].dropna().shape[0]
            if nonnull == 0 and orig_nonnull != 0:
                raise TypeError("Attempted to convert all string column {} to numeric".format(column_id))
    elif issubclass(new_type, Datetime):
        format = kwargs.get("format", None)
        # TODO: if float convert to int?
        if isinstance(df, dd.DataFrame):
            df[column_id] = dd.to_datetime(df[column_id], format=format,
                                           infer_datetime_format=True)
        else:
            df[column_id] = pd.to_datetime(df[column_id], format=format,
                                           infer_datetime_format=True)
    elif new_type == Boolean:
        map_dict = {kwargs.get("true_val", True): True,
                    kwargs.get("false_val", False): False,
                    True: True,
                    False: False}
        # TODO: what happens to nans?
        df[column_id] = df[column_id].map(map_dict).astype(np.bool)
    elif not issubclass(new_type, Discrete):
        raise Exception("Cannot convert column %s to %s" %
                        (column_id, new_type))
    return df


def get_linked_vars(entity):
    """Return a list with the entity linked variables.
    """
    link_relationships = [r for r in entity.entityset.relationships
                          if r.parent_entity.id == entity.id or
                          r.child_entity.id == entity.id]
    link_vars = [v.id for rel in link_relationships
                 for v in [rel.parent_variable, rel.child_variable]
                 if v.entity.id == entity.id]
    return link_vars


def col_is_datetime(col):
    # check if dtype is datetime - use .head() when getting first value
    # in case column is a dask Series
    if (col.dtype.name.find('datetime') > -1 or
            (len(col) and isinstance(col.head(1).iloc[0], datetime))):
        return True

    # if it can be casted to numeric, it's not a datetime
    dropped_na = col.dropna()
    try:
        pd.to_numeric(dropped_na, errors='raise')
    except (ValueError, TypeError):
        # finally, try to cast to datetime
        if col.dtype.name.find('str') > -1 or col.dtype.name.find('object') > -1:
            try:
                pd.to_datetime(dropped_na, errors='raise')
            except Exception:
                return False
            else:
                return True

    return False


def replace_latlong_nan(values):
    """replace a single `NaN` value with a tuple: `(np.nan, np.nan)`"""
    return values.where(values.notnull(), pd.Series([(np.nan, np.nan)] * len(values)))


def generate_statistics(data, variable_types, return_dataframe=False):
    """Calculates statistics for given data.

    Args:
        data (ft.Entity/pd.DataFrame/dd.DataFrame): Input data. Supports featuretools
            Entity, pandas DataFrame or dask DataFrame.

        variable_types (dict[str -> Variable/str], optional):
            Keys are of variable ids and values are variable types or type_strings.

        ascending_time (bool, optional): Specify if recent or oldest values
            should be calculated when determine the highly frequent
            datetimes.

        return_dataframe (bool): Specify whether to return a dataframe with the
            statistics instead of a dictionary.

    Returns:
        dict[str -> dict[str -> int/float/str] : Statistics of the data. Key is the column
            name of the data, value is dict of the statistics
            information (which can have keys of `count`, `nunique`
            `max`, `min`, `mode`, `mean`, `std`, `variable_type`, `nan_count`,
            `first_quartile`, `second_quartile`, `third_quartile`, `num_false`
            `num_true`)
    """
    from featuretools import (
        Entity,
        EntitySet
    )
    if isinstance(data, EntitySet):
        raise TypeError('Invalid data type. Please specify the specific entity.')

    if isinstance(data, dd.DataFrame):
        data = data.compute()

    if isinstance(data, Entity):
        data = data.df

    COMMON_STATISTICS = ["count", "nunique"]
    DATETIME_STATISTICS = ["max", "min", "mean"]
    NUMERIC_STATISTICS = ["max", "min", "mean", "std"]
    variable_types = convert_vtypes(variable_types)
    statistics = {}
    for column_name, v_type in variable_types.items():
        values = {}
        column = data.reset_index()[column_name]
        if v_type == Boolean:
            column = column.astype(bool)
            values["num_false"] = column.value_counts().get(False, 0)
            values["num_true"] = column.value_counts().get(True, 0)
        elif v_type == Numeric:
            column = column.astype(float)
            values.update(column.agg(NUMERIC_STATISTICS).to_dict())
            quant_values = column.quantile([0.25, 0.5, 0.75]).tolist()
            values["first_quartile"] = quant_values[0]
            values["second_quartile"] = quant_values[2]
            values["third_quartile"] = quant_values[2]
        elif v_type == Discrete or issubclass(v_type, Discrete):
            column = column.astype("category")
        elif v_type == Datetime:
            column = pd.to_datetime(column)
            values.update(column.agg(DATETIME_STATISTICS).to_dict())
        applicable = COMMON_STATISTICS
        if isinstance(v_type, LatLong):
            applicable = applicable.remove('nunique')
        values.update(column.agg(applicable).to_dict())
        values["nan_count"] = column.isna().sum()
        mode_values = column.mode()
        if mode_values is not None and len(mode_values) > 0:
            values["mode"] = mode_values[0]
        values['variable_type'] = v_type.type_string
        statistics[column_name] = values
    if return_dataframe:
        df = pd.DataFrame.from_dict(statistics)
        df = df.fillna(value='')
        return df
    return statistics
