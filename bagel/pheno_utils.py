import warnings
from collections import defaultdict
from typing import Union

import isodate
import jsonschema
import pandas as pd
import pydantic

from bagel import dictionary_models, mappings, models

DICTIONARY_SCHEMA = dictionary_models.DataDictionary.schema()


def generate_context():
    # Direct copy of the dandi-schema context generation function
    # https://github.com/dandi/dandi-schema/blob/c616d87eaae8869770df0cb5405c24afdb9db096/dandischema/metadata.py
    field_preamble = {
        "bg": "http://neurobagel.org/vocab/",
        "snomed": "https://identifiers.org/snomedct:",
        "nidm": "http://purl.org/nidash/nidm#",
        "cogAtlas": "https://www.cognitiveatlas.org/task/id/",
    }
    fields = {}
    for val in dir(models):
        klass = getattr(models, val)
        if not isinstance(klass, pydantic.main.ModelMetaclass):
            continue
        fields[klass.__name__] = "bg:" + klass.__name__
        for name, field in klass.__fields__.items():
            if name == "schemaKey":
                fields[name] = "@type"
            elif name == "identifier":
                fields[name] = "@id"
            elif name not in fields:
                fields[name] = {"@id": "bg:" + name}

    field_preamble.update(**fields)

    return {"@context": field_preamble}


def get_columns_about(data_dict: dict, concept: str) -> list:
    """
    Returns column names that have been annotated as "IsAbout" the desired concept.
    Parameters
    ----------
    data_dict: dict
        A valid Neurobagel annotated data dictionary must be provided.
    concept: str
        A (shorthand) IRI for a concept that a column can be "about"

    Returns
    list
        List of column names that are "about" the desired concept

    -------

    """
    return [
        col
        for col, annotations in data_dict.items()
        if annotations["Annotations"]["IsAbout"]["TermURL"] == concept
    ]


def map_categories_to_columns(data_dict: dict) -> dict:
    """
    Maps all pre-defined Neurobagel categories (e.g. "Sex") to a list of column names (if any) that
    have been linked to this category.
    """
    return {
        cat_name: get_columns_about(data_dict, cat_iri)
        for cat_name, cat_iri in mappings.NEUROBAGEL.items()
        if get_columns_about(data_dict, cat_iri)
    }


def map_tools_to_columns(data_dict: dict) -> dict:
    """
    Return a mapping of all assessment tools described in the data dictionary to the columns that
    are mapped to it.
    """
    out_dict = defaultdict(list)
    for col, content in data_dict.items():
        part_of = content["Annotations"].get("IsPartOf")
        if part_of is not None:
            out_dict[part_of.get("TermURL")].append(col)

    return out_dict


def is_missing_value(
    value: Union[str, int], column: str, data_dict: dict
) -> bool:
    """Determine if a raw value is listed as a missing value in the data dictionary entry for this column"""
    return value in data_dict[column]["Annotations"].get("MissingValues", [])


def is_column_categorical(column: str, data_dict: dict) -> bool:
    """Determine whether a column in a Neurobagel data dictionary is categorical"""
    if "Levels" in data_dict[column]:
        return True
    return False


def map_cat_val_to_term(
    value: Union[str, int], column: str, data_dict: dict
) -> str:
    """Take a raw categorical value and return the controlled term it has been mapped to"""
    return data_dict[column]["Annotations"]["Levels"][value]["TermURL"]


def get_age_heuristic(column: str, data_dict: dict) -> str:
    return data_dict[column]["Annotations"]["Transformation"]["TermURL"]


def transform_age(value: str, heuristic: str) -> float:
    is_recognized_heuristic = True
    try:
        if heuristic in ["bg:float", "bg:int"]:
            return float(value)
        if heuristic == "bg:euro":
            return float(value.replace(",", "."))
        if heuristic == "bg:bounded":
            return float(value.strip("+"))
        if heuristic == "bg:range":
            a_min, a_max = value.split("-")
            return (float(a_min) + float(a_max)) / 2
        if heuristic == "bg:iso8601":
            if not value.startswith("P"):
                value = "P" + value
            duration = isodate.parse_duration(value)
            return float(duration.years + duration.months / 12)
        else:
            is_recognized_heuristic = False
    except (ValueError, isodate.isoerror.ISO8601Error) as e:
        raise ValueError(
            f"There was a problem with applying the age transformation: {heuristic}. "
            "Check that the specified transformation is correct for the age values in your data dictionary."
        ) from e
    if not is_recognized_heuristic:
        raise ValueError(
            f"The provided data dictionary contains an unrecognized age transformation: {heuristic}. "
            "Ensure that the transformation TermURL is one of "
            '["bg:float", "bg:int", "bg:euro", "bg:bounded", "bg:range", "bg:iso8601"].'
        )


def get_transformed_values(
    columns: list, row: pd.Series, data_dict: dict
) -> Union[str, None]:
    """Convert a raw phenotypic value to the corresponding controlled term"""
    transf_val = []
    # TODO: implement a way to handle cases where more than one column contains information
    for col in columns[:1]:
        value = row[col]
        if is_missing_value(value, col, data_dict):
            continue
        if is_column_categorical(col, data_dict):
            transf_val.append(map_cat_val_to_term(value, col, data_dict))
        else:
            # TODO: replace with more flexible solution when we have more
            # continuous variables than just age
            transf_val.append(
                transform_age(str(value), get_age_heuristic(col, data_dict))
            )

    # TODO: once we can handle multiple columns, this section should be removed
    # and we should just return an empty list if no transform can be generated
    if not transf_val:
        return None
    return transf_val[0]


def are_not_missing(columns: list, row: pd.Series, data_dict: dict) -> bool:
    """
    Checks that all values in the specified columns are not missing values. This is mainly useful
    to determine the availability of an assessment tool
    """
    return all(
        [
            not is_missing_value(value, column, data_dict)
            for column, value in row[columns].items()
        ]
    )


def are_inputs_compatible(data_dict: dict, pheno_df: pd.DataFrame) -> bool:
    """
    Determines whether the provided data dictionary and phenotypic file make sense together
    """
    return all([key in pheno_df.columns for key in data_dict.keys()])


def find_undefined_cat_col_values(
    data_dict: dict, pheno_df: pd.DataFrame
) -> dict:
    """
    Checks that all categorical column values have annotations. Returns a dictionary containing
    any categorical column names and specific column values not defined in the corresponding data
    dictionary entry.
    """
    all_undefined_values = {}
    for col, attr in data_dict.items():
        if is_column_categorical(col, data_dict):
            known_values = list(attr["Levels"].keys()) + attr[
                "Annotations"
            ].get("MissingValues", [])
            unknown_values = []
            for value in pheno_df[col].unique():
                if value not in known_values:
                    unknown_values.append(value)
            if unknown_values:
                all_undefined_values[col] = unknown_values

    return all_undefined_values


def find_unused_missing_values(
    data_dict: dict, pheno_df: pd.DataFrame
) -> dict:
    """
    Checks if missing values annotated in the data dictionary appear at least once in the phenotypic file.
    Returns a dictionary containing any column names and annotated missing values not found in the phenotypic
    file column.
    """
    all_unused_missing_vals = {}
    for col, attr in data_dict.items():
        unused_missing_vals = []
        for missing_val in attr["Annotations"].get("MissingValues", []):
            if missing_val not in pheno_df[col].unique():
                unused_missing_vals.append(missing_val)
        if unused_missing_vals:
            all_unused_missing_vals[col] = unused_missing_vals

    return all_unused_missing_vals


def get_rows_with_empty_strings(df: pd.DataFrame, columns: list) -> list:
    """For specified columns, returns the indices of rows with empty strings"""
    # NOTE: Profile this section if things get slow, transforming "" -> nan and then
    # using .isna() will very likely be much faster
    empty_row = df[columns].applymap(lambda cell: cell == "").apply(lambda row: any([value for value in row]), axis=1)
    return list(empty_row[empty_row].index)


def validate_inputs(data_dict: dict, pheno_df: pd.DataFrame) -> None:
    """Determines whether input data are valid"""
    try:
        jsonschema.validate(data_dict, DICTIONARY_SCHEMA)
    except jsonschema.ValidationError as e:
        raise ValueError(
            "The provided data dictionary is not a valid Neurobagel data dictionary. "
            "Make sure that each annotated column contains an 'Annotations' key."
        ) from e

    # TODO: remove this validation when we start handling multiple participant and / or session ID columns
    if (
        len(
            get_columns_about(
                data_dict, concept=mappings.NEUROBAGEL["participant"]
            )
        )
        > 1
    ) | (
        len(
            get_columns_about(
                data_dict, concept=mappings.NEUROBAGEL["session"]
            )
        )
        > 1
    ):
        raise ValueError(
            "The provided data dictionary has more than one column about participant ID or session ID."
            "Please make sure that only one column is annotated for participant and session IDs."
        )

    if not are_inputs_compatible(data_dict, pheno_df):
        raise LookupError(
            "The provided data dictionary and phenotypic file are individually valid, "
            "but are not compatible. Make sure that you selected the correct data "
            "dictionary for your phenotypic file. Every column described in the data "
            "dictionary has to have a corresponding column with the same name in the "
            "phenotypic file"
        )

    undefined_cat_col_values = find_undefined_cat_col_values(
        data_dict, pheno_df
    )
    if undefined_cat_col_values:
        raise LookupError(
            "Categorical column(s) in the phenotypic file have values not annotated in the data dictionary "
            f"(shown as <column_name>: [<undefined values>]): {undefined_cat_col_values}. "
            "Please check that the correct data dictionary has been selected or make sure to annotate the missing values."
        )

    unused_missing_values = find_unused_missing_values(data_dict, pheno_df)
    if unused_missing_values:
        warnings.warn(
            "The following values annotated as missing values in the data dictionary were not found "
            "in the corresponding phenotypic file column(s) (<column_name>: [<unused missing values>]): "
            f"{unused_missing_values}. If this is not intentional, please check your data dictionary "
            "and phenotypic file."
        )

    # TODO: see if we can save ourselves the call to map_categories_to_columns here.
    # We cannot do the call earlier in the CLI (because it might fail for data invalid dictionaries)
    # and we need to know the column mappings in order to do the subject and session validation
    column_map = map_categories_to_columns(data_dict)
    columns_about_ids = column_map.get("participant", []) + column_map.get("session", [])
    if row_indices := get_rows_with_empty_strings(pheno_df, columns_about_ids):
        raise LookupError(
            "We have detected missing values in participant or session id columns. "
            "Please make sure that every row has a non-empty participant id (and session id where applicable)."
            f"We found missing values in the following rows (first row is zero): {row_indices}."
        )
