import os
import sys

import pytest

# Allow imports from pipelines/drift/ without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("PYSPARK_PYTHON", "/usr/bin/python3")
os.environ.setdefault("PYSPARK_DRIVER_PYTHON", "/usr/bin/python3")


@pytest.fixture(scope="session")
def spark():
    from pyspark.sql import SparkSession

    session = (
        SparkSession.builder.appName("sentinel-drift-tests")
        .master("local[2]")
        .config("spark.sql.shuffle.partitions", "4")
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    yield session
    session.stop()


def scores_df(spark, scores: list[float]):
    """Helper: build a single-column DataFrame from a list of floats."""
    return spark.createDataFrame([(s,) for s in scores], ["score"])
