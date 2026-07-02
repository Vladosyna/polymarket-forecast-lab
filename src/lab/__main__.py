"""Enable `python -m lab ...` as an alias for the `lab` console script."""

from lab.cli import app

if __name__ == "__main__":
    app()
