"""CLI entry point — run via `python -m pipelines.optimizer`."""

import argparse

from pipelines.optimizer.pipeline import run

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ONNX optimization pipeline")
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--log-dir", default="logs")
    parser.add_argument("--opset", type=int, default=17)
    args = parser.parse_args()

    run(args.model_id, args.output_dir, args.log_dir, args.opset)
