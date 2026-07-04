"""CLI entry point — run via `python -m pipelines.retraining`."""

import argparse
import logging
import os
import sys

from pipelines.retraining.pipeline import run

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Retraining pipeline")
    parser.add_argument(
        "--mongo-uri",
        default=os.environ.get("MONGO_URI", "mongodb://sentinel:sentinel@localhost:27017/sentinel"),
    )
    parser.add_argument(
        "--base-model-id",
        default=os.environ.get("BASE_MODEL_ID", "VijayRam1812/content-classifier-roberta"),
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--log-dir", default="logs")
    parser.add_argument(
        "--initial-dataset-path",
        default=os.environ.get("INITIAL_DATASET_PATH"),
        help="Optional CSV (raw_text,label columns) to sample alongside accepted flagged_content",
    )
    parser.add_argument("--sample-size", type=int, default=500)
    parser.add_argument("--epochs", type=int, default=3)
    args = parser.parse_args()

    # Explicit try/except + logger.exception rather than letting the
    # default excepthook print the traceback — guarantees a clearly
    # greppable "Retraining pipeline failed" banner around it, independent
    # of any stdout buffering quirks (see disable_tqdm in train.py, a
    # similar issue: unbuffered but line-oriented log capture upstream —
    # kubectl logs -f, Airflow's pod_manager — behaves better with plain
    # logging calls than mixed-mode stdout).
    try:
        run(
            mongo_uri=args.mongo_uri,
            base_model_id=args.base_model_id,
            output_dir=args.output_dir,
            log_dir=args.log_dir,
            initial_dataset_path=args.initial_dataset_path,
            sample_size=args.sample_size,
            epochs=args.epochs,
        )
    except Exception:
        logger.exception("Retraining pipeline failed")
        sys.exit(1)
