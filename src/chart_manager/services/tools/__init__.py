"""Developer-toolchain provisioning: the binaries chart-manager shells out to.

`install.py` lived under `services/validate/` because `validate deps-install`
is the CLI verb that drives it. That was a filing mistake, not a dependency:
nothing in `services/validate/` imports it, and one of the tools it installs
is `uv` -- the package manager for this project itself, which the validate
pipeline never invokes.

The CLI verb is unchanged; only the module's address moved.
"""
