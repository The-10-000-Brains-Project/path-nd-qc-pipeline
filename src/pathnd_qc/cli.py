"""Installed command dispatcher, keeping setup independent of scientific imports."""
import os
import sys


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "setup":
        from pathnd_qc.external.manager import main as setup
        return setup(args[1:])
    if args and args[0] == "batch":
        from pathnd_qc.batch.cli import main as batch
        return batch(args[1:])
    if args == ["--version"]:
        from pathnd_qc import __version__
        print(__version__)
        return 0
    if args and args[0] == "run":
        args.pop(0)
    if args in (["--help"], ["-h"]):
        print("Path-ND QC: pathnd-qc [run] --slide PATH [options]\n"
              "Batch: pathnd-qc batch run --slide_dir DIR --out DIR [options]\n"
              "Models: pathnd-qc setup --help\n")
    # The batch parent sets the budget before numerical libraries are imported.
    if "PATHND_CPU_THREADS" in os.environ:
        threads = int(os.environ["PATHND_CPU_THREADS"])
        if threads < 1:
            raise ValueError("PATHND_CPU_THREADS must be a positive integer")
        import torch
        torch.set_num_threads(threads)
        torch.set_num_interop_threads(1)
    from pathnd_qc.pipeline import main as run
    return run(args)
