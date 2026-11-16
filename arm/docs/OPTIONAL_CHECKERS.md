# Optional identity checkers

Skip this document for the default setup. All vision entry points default to
`--vlm none`, using SAM 3 directly through Python. No model server or checker API
package is required. The arm controller's HTTP connection is separate from any
model service.

Legacy checker adapters remain opt-in for experiments:

- `--vlm ollama`: requires a separately installed/running Ollama service and
  `qwen2.5vl:7b`. Nothing in the normal setup installs or starts them.
- `--vlm claude`: install `requirements-checkers.txt`, configure your own API key,
  and explicitly select this backend. It sends candidate image crops to Anthropic
  and may incur API charges. Confirm that the configured model is available to you.

These checkers can also misidentify objects. An additional model opinion does not
replace calibration, measured gripper geometry, independent evaluation or physical
grasp tests. Historical evaluation notes may describe checker-enabled experiments;
they do not describe the current default deployment.
