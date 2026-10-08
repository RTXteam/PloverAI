# package marker. `pipeline` is the parent package; the actual modules
# live under `pipeline.code`. having both __init__.py files (here and
# in code/) means `python -m pipeline.code.runner` resolves cleanly
# from the project root, and editor jumps-to-symbol behave correctly.
