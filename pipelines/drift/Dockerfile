FROM apache/spark:3.5.3

USER root

# Wire up PySpark from the bundled zips in the Spark distribution.
# py4j and pyspark.zip are already present — no pip install needed for Spark.
ENV PYTHONPATH="/opt/spark/python/lib/pyspark.zip:/opt/spark/python/lib/py4j-0.10.9.7-src.zip"
ENV PYSPARK_PYTHON="/usr/bin/python3"
ENV PYSPARK_DRIVER_PYTHON="/usr/bin/python3"

RUN pip3 install --no-cache-dir "psycopg[binary]>=3.2"

COPY metrics.py db.py drift_job.py /opt/spark/work-dir/

USER spark
